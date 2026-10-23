"""树莓派侧 ROS 接入层。

把「相机 / 逆运动学 / 舵机下发 / 舵机回读」收敛成一个类，供视觉驱动
（``vision_driver.py``）与训练作业台（``web/server.py``）共用。

三条硬约束，全部来自厂商源码实测结论：

1. **不抢串口**。厂商 ``hiwonder_servo_manager`` 已持有舵机总线。我们只往
   ``/servo_controllers/port_id_1/multi_id_pos_dur`` 发控制消息（厂商
   ``hiwonder_servo_serialproxy`` 订阅它），舵机回读读厂商已发布的
   ``/servo_controllers/port_id_1/servo_states``（``ServoStateList``，
   含 ``voltage`` / ``position`` / ``error``）—— 全程与厂商节点共存。

2. **不 import 厂商的 bus_servo_control**。它在 import 时注册 SIGINT handler
   并调用 ``rospy.signal_shutdown()``；在 uvicorn 进程里会劫持优雅退出信号。
   这里自己发同一种消息（``MultiRawIdPosDur``），行为等价、无副作用。

3. **IK 复用厂商 ``kinematics.ArmIK``**。逆运动学是纯计算、无副作用；
   复用它可以保证解算结果与厂商程序完全一致，不做第二套实现。

rospy 全部**惰性导入**，mock 模式下完全不碰 ROS —— 因此本模块在
演示笔记本（无 ROS）上也能 import 并跑通作业台 UI。
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import socket
import threading
import time
from typing import Any

logger = logging.getLogger("oceanus.grasp.arm_io")


class ArmIoError(RuntimeError):
    """ROS 链路不可用，或舵机指令未能下发。"""


class ArmIo:
    """ROS 侧硬件接入层（真机 + mock 两种后端）。"""

    def __init__(self, cfg: dict[str, Any], mock: bool = False) -> None:
        arm = cfg.get("arm", {}) or {}
        self.mock = bool(mock or (cfg.get("web", {}) or {}).get("mock"))

        # ---------- 话题与 ID 映射 ----------
        self.servo_topic = str(
            arm.get("servo_topic", "/servo_controllers/port_id_1/multi_id_pos_dur")
        )
        self.servo_states_topic = str(
            arm.get("servo_states_topic", "/servo_controllers/port_id_1/servo_states")
        )
        self.joint_states_topic = str(arm.get("joint_states_topic", "/joint_states"))
        self.camera_topic = str(arm.get("camera_topic", "/usb_cam/image_raw"))
        # ★ USB/UVC 摄像头帧源（开发机 / 无 ROS 联调）。显式配了就用它，不再接
        #   ROS 相机话题。与 ``mock`` **分离**：mock 管执行端（舵机下发与回读），
        #   usb_camera 只管帧源 —— 这样「真摄像头画面 + 模拟舵机」这个开发机
        #   组合才成立；真机上两者都留空/为假，行为与改动前一致。
        cam = arm.get("usb_camera", None)
        self.usb_camera: int | None = None if cam is None else int(cam)

        self.gripper_servo = int(arm.get("gripper_servo", 1))
        self.wrist_servo = int(arm.get("wrist_servo", 2))
        self.ik_servos = [int(x) for x in (arm.get("ik_servos") or [3, 4, 5, 6])]
        # ★ 注意命名：脉宽字段带 _pulse 后缀，避免与同名方法
        #   （gripper_open() / gripper_close_adaptive()）相互覆盖 ——
        #   实例属性会遮蔽类方法，踩过一次就不再踩第二次。
        # ★ 夹爪口径已按厂商源码核对：**脉宽越大越闭**。
        #   warehouse/in.py：夹住 450 / 松开 75；Sensor/AIColorsorting.py：
        #   张开 = servo1-280、闭合 = servo1（servo1=500 → 220/500）。
        #   默认取保守的开 200 / 闭 450（薄膜袋自适应闭合通常夹不到 450 就停）。
        self.gripper_open_pulse = int(arm.get("gripper_open", 200))
        self.gripper_close_pulse = int(arm.get("gripper_close", 450))
        self.gripper_step = int(arm.get("gripper_step", 20))
        self.gripper_step_wait = float(arm.get("gripper_step_wait", 0.12))
        self.wrist_center = int(arm.get("wrist_center", 500))
        self.default_duration_ms = int(arm.get("default_duration_ms", 800))

        home = arm.get("home") or {}
        self.home_coord = [float(x) for x in (home.get("coord") or [0.0, 0.17, 0.30])]
        self.home_pitch = float(home.get("pitch", -65))
        pr = home.get("pitch_range") or [-180, 0]
        self.home_pitch_range = (float(pr[0]), float(pr[1]))

        # ---------- 运行态 ----------
        self._lock = threading.RLock()
        self._ros: Any = None
        self._pub: Any = None
        self._ik: Any = None
        self._frame: Any = None
        self._frame_ts: float = 0.0
        #: USB 摄像头帧源运行态（取帧常驻线程 + 采集句柄）
        self._cap: Any = None
        self._cam_thread: threading.Thread | None = None
        self._cam_stop = threading.Event()
        self._states: dict[str, dict[str, Any]] = {}
        self._started = False
        self._last_pulses: dict[int, int] = {}
        #: mock 专用：是否已"夹住"目标（夹住后合成画面不再画那个袋）
        self._mock_grabbed = False
        #: ROS 接入失败原因。非 None 表示"服务起来了但硬件链路没通"。
        #: ★ 为什么不让它抛：现场最常见的故障是 ROS 没起来（忘了 source /
        #:   厂商节点没启动）。此时整个作业台崩掉、页面打不开是最糟的失败方式 ——
        #:   正确做法是服务照常起，页面明确显示排障提示（与 ArmBridge
        #:   "读不到遥测不让链路挂掉"同一条思路）。
        self.ros_error: str | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """建节点、订阅相机与舵机状态；mock 模式下什么都不做。

        ROS 接入失败**不抛出** —— 只记到 ``ros_error``，让 Web 服务照常起来。
        """
        if self._started:
            return
        self._started = True
        # 帧源优先：配了 USB 摄像头就用它，不接 ROS 相机。
        # 摄像头打不开**不抛**（与 ROS 接入失败同一处理）—— 记到 ros_error，
        # 让作业台照常打开并显示排障提示，而不是整页打不开。
        if self.usb_camera is not None:
            try:
                self._start_usb_camera()
            except ArmIoError as exc:
                self.ros_error = str(exc)
                logger.error("USB 摄像头帧源不可用（作业台仍可打开，但无画面）：%s", exc)
            return
        if self.mock:
            logger.info("ArmIo 以 mock 模式启动（不接 ROS）")
            return
        try:
            self._ensure_ros()
        except ArmIoError as exc:
            self.ros_error = str(exc)
            logger.error("ROS 接入失败（作业台仍可打开，但无画面/无舵机）：%s", exc)
            return
        if self.ros_error is None:
            logger.info(
                "ArmIo 已接入 ROS：camera=%s servo=%s states=%s",
                self.camera_topic, self.servo_topic, self.servo_states_topic,
            )

    def stop(self) -> None:
        self._started = False
        self._cam_stop.set()
        if self._cam_thread is not None:
            self._cam_thread.join(timeout=1.0)
            self._cam_thread = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:  # noqa: BLE001
                logger.warning("摄像头释放失败", exc_info=True)
            self._cap = None

    # ------------------------------------------------------------------
    # USB 摄像头帧源（与 ROS 相机话题二选一）
    # ------------------------------------------------------------------
    def _start_usb_camera(self) -> None:
        """打开 USB/UVC 摄像头并起常驻取帧线程。

        Windows 优先 DSHOW，其余情况退回默认后端；两者都失败才判定打不开。
        """
        try:
            import cv2
        except ImportError as exc:
            raise ArmIoError(
                "USB 摄像头帧源需要 opencv-python（pip install opencv-python）"
            ) from exc
        idx = int(self.usb_camera)
        cap = None
        for backend in (cv2.CAP_DSHOW, cv2.CAP_ANY):
            candidate = cv2.VideoCapture(idx, backend)
            if candidate.isOpened():
                cap = candidate
                break
            candidate.release()
        if cap is None:
            raise ArmIoError(f"USB 摄像头 {idx} 打不开（被其它程序占用 / 索引不对）")
        self._cap = cap
        self._cam_stop.clear()
        self._cam_thread = threading.Thread(target=self._camera_loop, daemon=True)
        self._cam_thread.start()
        # ★ 必须等**第一帧真的到手**才算成功。Windows 上设备被别的进程独占时，
        #   ``VideoCapture.isOpened()`` 仍会返回 True，但 ``read()`` 一帧都出不来 ——
        #   不校验就会带着一个"永远读不到帧"的帧源一路跑下去，表现为闭环
        #   ``detecting × N → error``、日志里却查不出原因（与 MQTT 不等 CONNACK
        #   就 publish 是同一类静默缺陷）。等不到就明确报错，并说明成因。
        deadline = time.time() + 3.0
        while time.time() < deadline and self._frame is None:
            time.sleep(0.05)
        if self._frame is None:
            self._cam_stop.set()
            self._cam_thread.join(timeout=1.0)
            self._cam_thread = None
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                logger.warning("摄像头释放失败", exc_info=True)
            self._cap = None
            raise ArmIoError(
                f"USB 摄像头 {idx} 打开了但读不到帧：多半被其它程序独占 —— "
                f"同一颗摄像头一次只能被一个进程用，作业台与机械臂桥接层不要同时开"
            )
        logger.info("ArmIo 帧源 = USB 摄像头 index=%s（不接 ROS 相机）", idx)

    def _camera_loop(self) -> None:
        """持续取帧。

        ★ 为什么必须常驻读：``VideoCapture`` 内部有帧缓冲，只在
        ``latest_frame()`` 时才 ``read()``，拿到的会是缓冲区里最旧的那帧 ——
        视觉伺服会「看着几帧之前的画面」逼近，越走越偏。常驻读取保证
        latest 永远是当前帧。
        """
        while not self._cam_stop.is_set():
            cap = self._cap
            if cap is None:
                break
            try:
                ok, frame = cap.read()
            except Exception:  # noqa: BLE001
                ok, frame = False, None
            if ok and frame is not None:
                with self._lock:
                    self._frame = frame
                    self._frame_ts = time.time()
            else:
                time.sleep(0.05)

    # ------------------------------------------------------------------
    # ROS 惰性接入
    # ------------------------------------------------------------------
    def _ensure_ros(self) -> Any:
        if self._ros is not None:
            return self._ros
        try:
            import rospy
            from rospy import core as rospy_core
            from sensor_msgs.msg import Image
            from hiwonder_servo_msgs.msg import (
                MultiRawIdPosDur,
                RawIdPosDur,
                ServoStateList,
            )
        except ImportError as exc:  # pragma: no cover - 取决于运行环境
            raise ArmIoError(
                "ROS Python 环境不可用（rospy / sensor_msgs / hiwonder_servo_msgs）。"
                "请在树莓派上 `source <armpi_fpv>/devel/setup.bash` 后重启本程序；"
                "或在开发机上用 --mock 模式跑 UI。"
            ) from exc

        if not rospy_core.is_initialized():
            rospy.init_node("oceanus_grasp_io", anonymous=True, disable_signals=True)

        self._ros = rospy
        self._MultiRawIdPosDur = MultiRawIdPosDur
        self._RawIdPosDur = RawIdPosDur
        self._pub = rospy.Publisher(self.servo_topic, MultiRawIdPosDur, queue_size=1)
        rospy.Subscriber(self.camera_topic, Image, self._on_image, queue_size=1)
        rospy.Subscriber(
            self.servo_states_topic, ServoStateList, self._on_states, queue_size=1
        )
        # 给订阅一点时间拿首帧/首个状态，否则第一次读取会是空
        deadline = time.time() + 3.0
        while time.time() < deadline and self._frame is None:
            time.sleep(0.1)
        return rospy

    def _ensure_ik(self) -> Any:
        """复用厂商逆运动学（纯计算，无副作用）。"""
        if self._ik is not None:
            return self._ik
        try:
            from kinematics import ik_transform  # 厂商 armpi_fpv_kinematics
        except ImportError as exc:
            raise ArmIoError(
                "厂商 kinematics 不可用。请确认已 `source <armpi_fpv>/devel/setup.bash`；"
                "该包由厂商 ROS 工作空间提供，不由 pip 安装。"
            ) from exc
        self._ik = ik_transform.ArmIK()
        return self._ik

    # ------------------------------------------------------------------
    # 回调
    # ------------------------------------------------------------------
    def _on_image(self, msg: Any) -> None:  # pragma: no cover - 需 ROS
        try:
            import cv2
            import numpy as np
        except ImportError:
            return
        # 手动转换，不依赖 cv_bridge（厂商 face.py 同做法）
        image = np.ndarray(
            shape=(msg.height, msg.width, 3), dtype=np.uint8, buffer=msg.data
        )
        bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
        with self._lock:
            self._frame = bgr
            self._frame_ts = time.time()

    def _on_states(self, msg: Any) -> None:  # pragma: no cover - 需 ROS
        with self._lock:
            for st in getattr(msg, "servo_states", []) or []:
                self._states[str(st.id)] = {
                    "vin": round(float(st.voltage), 2),      # mV，与平台页面换算一致
                    "position": int(st.position),
                    "goal": int(st.goal),
                    "error": int(st.error),
                }

    # ------------------------------------------------------------------
    # 画面
    # ------------------------------------------------------------------
    def latest_frame(self) -> Any:
        """最新一帧 BGR 图像（np.ndarray）或 None。

        合成画面只在「mock 执行端且没有真摄像头帧源」时使用 —— 一旦配了
        USB 摄像头，画面就来自真实摄像头。
        """
        if self.mock and self.usb_camera is None:
            return self._mock_frame()
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def latest_jpeg(self, quality: int = 70) -> bytes | None:
        """最新一帧编码成 JPEG，供 MJPEG 推流。"""
        frame = self.latest_frame()
        if frame is None:
            return None
        try:
            import cv2
        except ImportError:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
        return buf.tobytes() if ok else None

    def _mock_frame(self) -> Any:
        """合成画面：深色水面 + 低饱和塑料袋 + 高饱和彩色浮标。

        用途是让检测器/状态机/UI 在没有硬件的机器上也能跑通 —— 合成图里
        刻意保留「袋低饱和、浮标高饱和」这一真实场景特征，好让抗浮标
        三层防线里的 HSV 那一层也能被验证。
        """
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise ArmIoError("mock 画面需要 numpy 与 opencv-python") from exc
        h, w = 480, 640
        img = np.full((h, w, 3), 42, dtype=np.uint8)          # 深色水面
        t = time.time()
        # 漂浮塑料袋：低饱和、白灰、缓慢漂移（袋口朝下，便于演示抓取点）
        # 已"夹住"就不再画 —— 模拟目标被取走，供抓取验证（原位消失）判定
        if not self._mock_grabbed:
            cx = int(w * 0.5 + 70 * math.sin(t * 0.30))
            cy = int(h * 0.62 + 18 * math.sin(t * 0.55))
            cv2.ellipse(img, (cx, cy), (72, 34), 0, 0, 360, (222, 226, 231), -1)
            cv2.ellipse(img, (cx, cy), (72, 34), 0, 0, 360, (190, 196, 203), 2)
        # 彩色浮标：高饱和、静止（抗干扰的负样本）
        for bx, by, color in ((150, 300, (0, 0, 235)), (520, 318, (235, 80, 0)),
                              (330, 196, (0, 205, 255))):
            cv2.circle(img, (bx, by), 26, color, -1)
            cv2.circle(img, (bx, by), 26, (255, 255, 255), 2)
        return img

    def frame_age(self) -> float:
        """最近一帧的年龄（秒）；合成画面恒为 0。"""
        if self.mock and self.usb_camera is None:
            return 0.0
        with self._lock:
            return 0.0 if not self._frame_ts else max(0.0, time.time() - self._frame_ts)

    # ------------------------------------------------------------------
    # 舵机下发
    # ------------------------------------------------------------------
    def set_servos(self, duration_ms: int, pairs: list[tuple[int, int]]) -> None:
        """下发一组 (舵机 id, 脉宽 0..1000)，时长 duration_ms。

        消息类型与厂商 ``bus_servo_control.set_servos`` 完全一致，只是
        在这里自己发，避免 import 厂商模块带来的 SIGINT 副作用。
        """
        clean: list[tuple[int, int]] = []
        for sid, pos in pairs:
            sid_i, pos_i = int(sid), int(pos)
            if not 0 <= pos_i <= 1000:
                raise ArmIoError(f"舵机脉宽必须在 0..1000 之间，收到 {sid_i}:{pos_i}")
            clean.append((sid_i, pos_i))
        if self.mock:
            with self._lock:
                for sid, pos in clean:
                    self._last_pulses[sid] = pos
            return
        # ROS 未通就明确报错，不反复重试 import、也不假装下发成功
        if self.ros_error:
            raise ArmIoError(f"ROS 链路不可用：{self.ros_error}")
        self._ensure_ros()
        if self._pub is None:
            raise ArmIoError("舵机发布器未就绪")
        msg = self._MultiRawIdPosDur(
            id_pos_dur_list=[
                self._RawIdPosDur(sid, pos, int(duration_ms)) for sid, pos in clean
            ]
        )
        self._pub.publish(msg)
        with self._lock:
            for sid, pos in clean:
                self._last_pulses[sid] = pos

    def set_servo(self, servo_id: int, position: int, duration_ms: int | None = None) -> None:
        """单舵机下发（页面手动点动用）。"""
        self.set_servos(duration_ms or self.default_duration_ms, [(servo_id, position)])

    def servo_telemetry(self) -> dict[str, dict[str, Any]]:
        """逐舵机回读 {id: {vin, position, goal, error}}。

        真机走厂商 ``servo_states`` 话题；mock 模式用最近一次下发值合成，
        让页面与平台遥测面板在无硬件时也非空。
        """
        if self.mock:
            with self._lock:
                out: dict[str, dict[str, Any]] = {}
                for sid in (self.gripper_servo, self.wrist_servo, *self.ik_servos):
                    out[str(sid)] = {
                        "vin": 7400.0,
                        "position": int(self._last_pulses.get(sid, 500)),
                        "goal": int(self._last_pulses.get(sid, 500)),
                        "error": 0,
                    }
                return out
        with self._lock:
            return {k: dict(v) for k, v in self._states.items()}

    def last_pulse(self, servo_id: int) -> int | None:
        with self._lock:
            return self._last_pulses.get(int(servo_id))

    # ------------------------------------------------------------------
    # 运动学动作
    # ------------------------------------------------------------------
    def move_to(
        self,
        x: float,
        y: float,
        z: float,
        pitch: float | None = None,
        pitch_range: tuple[float, float] | None = None,
        duration_ms: int | None = None,
        extra_pairs: list[tuple[int, int]] | None = None,
    ) -> bool:
        """IK 解算到目标坐标并下发。

        与厂商 face.py 的差别：那里的脉宽是录制值，这里每一帧都由
        ``ArmIK.setPitchRanges()`` 现算 —— 这正是「非预设动作」的落点。
        无解（目标不可达）返回 False，由调用方决定换目标还是重试。
        """
        pitch = self.home_pitch if pitch is None else float(pitch)
        rng = pitch_range or self.home_pitch_range
        if self.mock:
            # mock 模式不真解 IK（避免依赖厂商包），用脉宽近似占位
            base = [(sid, 500) for sid in self.ik_servos]
            self.set_servos(duration_ms or self.default_duration_ms,
                            (extra_pairs or []) + base)
            return True
        if self.ros_error:
            raise ArmIoError(f"ROS 链路不可用：{self.ros_error}")
        ik = self._ensure_ik()
        result = ik.setPitchRanges((float(x), float(y), float(z)), float(pitch),
                                   float(rng[0]), float(rng[1]))
        if not result:
            return False
        _angles, servos, _alpha = result
        pairs: list[tuple[int, int]] = list(extra_pairs or [])
        for sid in self.ik_servos:
            key = f"servo{sid}"
            if key in servos:
                pairs.append((sid, int(servos[key])))
        self.set_servos(duration_ms or self.default_duration_ms, pairs)
        return True

    def gripper_open(self, duration_ms: int | None = None) -> None:
        self._mock_grabbed = False
        self.set_servo(self.gripper_servo, self.gripper_open_pulse, duration_ms)

    def gripper_close_adaptive(
        self, stall_threshold: int = 8, max_steps: int = 24
    ) -> int:
        """夹爪渐进闭合，检测到位置停滞即认为「夹到东西」并停止。

        这是自适应夹取：袋子大小不一，固定脉宽回放会夹空或夹爆；
        改为小步闭合 + 回读停滞判断，夹住就停。返回停止时的脉宽。

        ★ 闭合**方向由脉宽决定**，不能写死加减号。厂商源码口径（已核对）：
          **脉宽越大越闭**——`warehouse/in.py` 夹住时 450、松开 75；
          `Sensor/AIColorsorting.py` 注释写明「爪子张开 = servo1-280、夹持器闭合
          = servo1」。所以 `gripper_open < gripper_close` 时每步要**加**。
          原先写死 `pos - step`，在真机上等于越夹越松。

        ★ 真机依赖 ``servo_states`` 的回读；读不到回读时退化为按步数走完
          （不会误判为「夹到了」）。
        ★ mock 模式：直接模拟「闭合到阻力位就停」，并把合成画面里的袋子
          移走 —— 这样抓取验证的双证据（原位消失 + 夹爪停滞）都能被走到。
        """
        start = int(self.gripper_open_pulse)
        target = int(self.gripper_close_pulse)
        step = abs(int(self.gripper_step)) * (1 if target >= start else -1)

        if self.mock:
            # 夹到袋子＝停在半途（离全闭还有一段），供停滞判据使用
            object_pulse = int(round(start + 0.55 * (target - start)))
            with self._lock:
                self._last_pulses[self.gripper_servo] = object_pulse
            self._mock_grabbed = True
            return object_pulse

        pos = start
        stall_count = 0
        prev_read: int | None = None
        for _ in range(max_steps):
            pos += step
            # 不越过全闭位（也防 step 方向写错时越界）
            if (step > 0 and pos > target) or (step < 0 and pos < target):
                pos = target
            self.set_servo(self.gripper_servo, pos, int(self.gripper_step_wait * 1000))
            time.sleep(self.gripper_step_wait)
            read = (self.servo_telemetry().get(str(self.gripper_servo)) or {}).get("position")
            if read is not None and prev_read is not None:
                if abs(int(read) - int(prev_read)) <= stall_threshold:
                    stall_count += 1
                    if stall_count >= 2:
                        break
                else:
                    stall_count = 0
            prev_read = read
            if pos == target:
                break
        return pos

    def home(self, duration_ms: int = 1500) -> bool:
        """回待命位（IK 现算，不是录制位）。"""
        return self.move_to(
            *self.home_coord,
            pitch=self.home_pitch,
            pitch_range=self.home_pitch_range,
            duration_ms=duration_ms,
            extra_pairs=[(self.wrist_servo, self.wrist_center)],
        )


class _RemoteLink:
    """一条与板上代理的「换行分隔 JSON」长连接（自带发送锁与读缓冲）。

    共享一条连接时并发调用会串话，因此所有请求都在 ``lock`` 内完成
    「发→收」一个来回；连接断开后下次调用自动重连（``connect`` 幂等）。
    """

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = float(timeout)
        self.sock: Any = None
        self.buf = b""
        self.lock = threading.RLock()
        self._seq = 0

    def connect(self) -> None:
        if self.sock is not None:
            return
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self.sock = sock
        self.buf = b""

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self.buf = b""

    def call(self, op: str, **params: Any) -> dict[str, Any]:
        with self.lock:
            try:
                self.connect()
                self._seq += 1
                payload: dict[str, Any] = {"id": self._seq, "op": op}
                payload.update(params)
                self.sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
                line = self._read_line()
            except (OSError, ValueError) as exc:
                self.close()
                raise ArmIoError(
                    "板上代理不可达（%s:%s）：%s" % (self.host, self.port, exc)
                ) from exc
        try:
            resp = json.loads(line)
        except ValueError as exc:
            raise ArmIoError("板上代理返回不可解析：%r" % line[:200]) from exc
        if not resp.get("ok"):
            raise ArmIoError(str(resp.get("error") or "板上代理返回失败"))
        return resp

    def _read_line(self) -> str:
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("连接被板上代理关闭")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("utf-8", "replace")


class ArmIoRemote(ArmIo):
    """把执行端留在树莓派：本机只持 TCP 客户端（对应板上 ``deploy/board_agent.py``）。

    为什么需要它：作业台（FastAPI）跑在开发机上，但机械臂只挂在树莓派的
    ROS 与舵机总线上 —— 开发机既没有 ROS、也碰不到那根总线。板上代理用一条
    换行分隔 JSON 的连接把「舵机下发 / 遥测回读 / 实时画面」暴露出来，本类与之
    对应。**接口与 `ArmIo` 逐条对齐**，所以作业台 / 抓取闭环 / 标定代码不用改。

    与 `ArmIo` 的两点差异：

    * ``usb_camera`` 是字符串哨兵而非索引 —— 下游用「帧源是否真实」来决定检测
      后端（真摄像头画面必须用真模型）。远端拿到的就是**真实摄像头画面**，
      所以这里必须让调用方认为「有真实相机」；相机在本机并不存在，故不留索引。
    * 夹爪自适应渐闭的**回读停滞判据在板上做**（板上 7Hz 直读 ``servo_states``，
      比「远端每步回读一次」延迟低得多），本机只收结果脉宽。
    """

    def __init__(self, cfg: dict[str, Any], mock: bool = False) -> None:
        super().__init__(cfg, mock=False)
        arm = cfg.get("arm", {}) or {}
        remote = arm.get("remote") or {}
        self.remote_host = str(
            os.environ.get("OCEANUS_ARM_HOST") or remote.get("host") or "192.168.149.1"
        )
        self.remote_port = int(
            os.environ.get("OCEANUS_ARM_PORT") or remote.get("port") or 8093
        )
        self.remote_timeout = float(remote.get("timeout") or 30.0)
        self.frame_period = float(remote.get("frame_period") or 0.12)
        self.frame_width = int(remote.get("frame_width") or 640)
        self.frame_quality = int(remote.get("frame_quality") or 70)

        self.usb_camera = "remote"
        self._cmd = _RemoteLink(self.remote_host, self.remote_port, self.remote_timeout)
        self._frame_link = _RemoteLink(
            self.remote_host, self.remote_port, min(8.0, self.remote_timeout)
        )
        self._remote_frame: Any = None
        self._remote_frame_ts = 0.0
        self._frame_stop = threading.Event()
        self._frame_thread: threading.Thread | None = None
        self._cached_states: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        # 取帧线程**无论 ping 成败都起**：链路断了它会自己重连，代理后起来也能自愈。
        self._frame_stop.clear()
        self._frame_thread = threading.Thread(target=self._frame_loop, daemon=True)
        self._frame_thread.start()
        try:
            self._call("ping")
        except ArmIoError as exc:
            logger.error("板上代理不可达（作业台仍可打开，但无画面/无舵机）：%s", exc)
            return
        logger.info("ArmIo 已接入板上代理 %s:%s", self.remote_host, self.remote_port)

    def stop(self) -> None:
        self._started = False
        self._frame_stop.set()
        if self._frame_thread is not None:
            self._frame_thread.join(timeout=1.5)
            self._frame_thread = None
        self._cmd.close()
        self._frame_link.close()

    # ------------------------------------------------------------------
    # 链路状态
    # ------------------------------------------------------------------
    def _mark(self, ok: bool, message: str | None = None) -> None:
        """把「最近一次链路结果」记进 ``ros_error``（作业台顶栏据此显红胶囊）。"""
        if ok:
            if self.ros_error:
                self.ros_error = None
        else:
            self.ros_error = message

    def _call(self, op: str, **params: Any) -> dict[str, Any]:
        try:
            resp = self._cmd.call(op, **params)
        except ArmIoError as exc:
            self._mark(False, str(exc))
            raise
        self._mark(True)
        return resp

    # ------------------------------------------------------------------
    # 画面（板上取帧线程 → 本机解码）
    # ------------------------------------------------------------------
    def _frame_loop(self) -> None:
        import cv2
        import numpy as np

        while not self._frame_stop.is_set():
            try:
                resp = self._frame_link.call(
                    "frame", max_width=self.frame_width, quality=self.frame_quality
                )
                raw = base64.b64decode(resp["jpeg_b64"])
                frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is not None:
                    with self._lock:
                        self._remote_frame = frame
                        self._remote_frame_ts = time.time()
                    self._mark(True)
                else:
                    self._mark(False, "远端帧解码失败（JPEG 数据损坏）")
            except ArmIoError as exc:
                self._mark(False, str(exc))
                time.sleep(1.0)
                continue
            except Exception:  # noqa: BLE001
                logger.exception("远端取帧异常")
                time.sleep(0.5)
                continue
            time.sleep(self.frame_period)

    def latest_frame(self) -> Any:
        with self._lock:
            return None if self._remote_frame is None else self._remote_frame.copy()

    def frame_age(self) -> float:
        with self._lock:
            ts = self._remote_frame_ts
        return 0.0 if not ts else max(0.0, time.time() - ts)

    # ------------------------------------------------------------------
    # 舵机下发 / 便读
    # ------------------------------------------------------------------
    def set_servos(self, duration_ms: int, pairs: list[tuple[int, int]]) -> None:
        clean: list[list[int]] = []
        for sid, pos in pairs:
            sid_i, pos_i = int(sid), int(pos)
            if not 0 <= pos_i <= 1000:
                raise ArmIoError(f"舵机脉宽必须在 0..1000 之间，收到 {sid_i}:{pos_i}")
            clean.append([sid_i, pos_i])
        self._call("set_servos", duration_ms=int(duration_ms), pairs=clean)
        with self._lock:
            for sid_i, pos_i in clean:
                self._last_pulses[sid_i] = pos_i

    def servo_telemetry(self) -> dict[str, dict[str, Any]]:
        try:
            resp = self._call("telemetry")
        except ArmIoError as exc:
            logger.warning("远端遥测不可用，回退到最近一次成功值：%s", exc)
            with self._lock:
                return {k: dict(v) for k, v in self._cached_states.items()}
        servos = {str(k): dict(v) for k, v in (resp.get("servos") or {}).items()}
        with self._lock:
            self._cached_states = servos
        return servos

    # ------------------------------------------------------------------
    # 运动学动作
    # ------------------------------------------------------------------
    def move_to(
        self,
        x: float,
        y: float,
        z: float,
        pitch: float | None = None,
        pitch_range: tuple[float, float] | None = None,
        duration_ms: int | None = None,
        extra_pairs: list[tuple[int, int]] | None = None,
    ) -> bool:
        pitch = self.home_pitch if pitch is None else float(pitch)
        rng = pitch_range or self.home_pitch_range
        resp = self._call(
            "move_to",
            x=float(x),
            y=float(y),
            z=float(z),
            pitch=float(pitch),
            pitch_range=[float(rng[0]), float(rng[1])],
            duration_ms=int(duration_ms or self.default_duration_ms),
            extra_pairs=[[int(a), int(b)] for a, b in (extra_pairs or [])],
        )
        return bool(resp.get("reachable"))

    def gripper_open(self, duration_ms: int | None = None) -> None:
        self._call(
            "gripper_open",
            duration_ms=int(duration_ms or self.default_duration_ms),
            pulse=self.gripper_open_pulse,
        )
        with self._lock:
            self._last_pulses[self.gripper_servo] = self.gripper_open_pulse

    def gripper_close_adaptive(
        self, stall_threshold: int = 8, max_steps: int = 24
    ) -> int:
        """渐闭 + 回读停滞判据 —— 判据在板上跑（直读 `servo_states`，延迟最低）。"""
        resp = self._call(
            "gripper_close",
            stall_threshold=int(stall_threshold),
            max_steps=int(max_steps),
            open_pulse=self.gripper_open_pulse,
            close_pulse=self.gripper_close_pulse,
            step=self.gripper_step,
            step_wait=self.gripper_step_wait,
        )
        pulse = int(resp.get("pulse") or self.gripper_close_pulse)
        with self._lock:
            self._last_pulses[self.gripper_servo] = pulse
        return pulse

    def home(self, duration_ms: int = 1500) -> bool:
        resp = self._call("home", duration_ms=int(duration_ms))
        return bool(resp.get("reachable"))


def build_arm_io(cfg: dict[str, Any], mock: bool = False) -> ArmIo:
    """选执行端：配了 ``arm.remote.enabled`` 走板上代理，否则本机 ROS / mock。

    远端模式存在的唯一理由：作业台跑在开发机上，而机械臂挂在树莓派上。
    """
    arm = cfg.get("arm", {}) or {}
    remote = arm.get("remote") or {}
    if not mock and remote.get("enabled"):
        return ArmIoRemote(cfg, mock=False)
    return ArmIo(cfg, mock=mock)
