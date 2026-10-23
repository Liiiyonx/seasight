"""全自动抓取闭环（六态状态机）。

## 与厂商 demo 的本质区别

厂商的 ``face_detect`` / ``warehouse`` 是「预设初始位姿 + 固定关节序列回放」：
每个脉宽都是录制值。本模块里**每一个动作都由感知输出经 IK 现算**：

    YOLO 检出 → 像素↔世界坐标换算 → ArmIK 解算 → 总线舵机下发

没有一段录制序列，换目标位置、换袋子大小都照样工作。

## 六态

    ① 持续检测 → ② 目标锁定（滤浮标）→ ③ 视觉伺服逼近
      → ④ 精准夹取 → ⑤ 抓取验证 → ⑥ 放置入槽 →（循环回 ①）

⑥ 的落点是**臂座旁边的料槽**（side_hopper，见 ``agent.place``）：抓取点每帧
由视觉现算，放置点是固定工位，示教/配置一次即可（``coord`` 走 IK，``pose``
回放示教位姿）。**没有放置点就直接报错**，不会把袋子丢在原地假装完成。

失败任一步就回 ① 重试，超过 ``max_retry`` 则上报异常。

## 抗浮标三层防线在代码里的落点

* 训练层 —— 浮标作 ``buoy`` 显式负类（在 ``vision/dataset`` + 训练侧）
* 推理层 —— 仅 ``plastic_bag`` 且过阈值才成候选（``Detector.targets``）
* 决策层 —— 本模块 ``_lock_target()`` 里的 HSV 饱和度辅助校验 + 轨迹邻域锁定

本模块被两个入口复用（一份实现、两处调用）：

* ``vision_driver.OceanusVisionArmDriver``（桥接进程，随平台派单执行）
* ``web/server.py``（作业台进程，页面「开始作业」按钮）
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .calibration import describe_place

logger = logging.getLogger("oceanus.grasp.loop")

#: 状态机相位名（页面与日志共用同一套词，避免两处漂移）
PHASES = ("idle", "detecting", "locking", "servoing", "grasping",
          "verifying", "placing", "done", "error")

PhaseCb = Callable[[str, dict[str, Any]], None]


@dataclass
class GraspOutcome:
    """一次抓取的结果。"""

    ok: bool
    phase: str
    attempts: int = 0
    detected: int = 0
    grip_pulse: int | None = None
    target_gone: bool = False
    grip_stalled: bool = False
    #: 是否真的放进了料槽。★ 与 ``ok`` 分开：夹住了但放置点不可达时
    #: ``ok=True / placed=False`` —— 这两种结果的处置方式完全不同
    #: （前者可以继续下一单，后者要人工去把臂上的袋子取下来）。
    placed: bool = False
    message: str = ""
    log: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "phase": self.phase,
            "attempts": self.attempts,
            "detected": self.detected,
            "grip_pulse": self.grip_pulse,
            "target_gone": self.target_gone,
            "grip_stalled": self.grip_stalled,
            "placed": self.placed,
            "message": self.message,
        }


class GraspLoopError(RuntimeError):
    """闭环前置条件不满足（无标定 / 无模型）。"""


class GraspLoop:
    """视觉闭环抓取（无预设动作）。"""

    def __init__(
        self,
        io: Any,
        detector: Any,
        calibration: Any,
        cfg: dict[str, Any],
        *,
        on_phase: PhaseCb | None = None,
    ) -> None:
        agent = cfg.get("agent", {}) or {}
        arm = cfg.get("arm", {}) or {}
        self.io = io
        self.detector = detector
        self.cal = calibration
        self.on_phase = on_phase

        self.frame_period_s = float(agent.get("frame_period_ms", 250)) / 1000.0
        self.max_retry = int(agent.get("max_retry", 3))
        self.depth_offset = float(agent.get("grasp_depth_offset", 0.005))
        self.approach_height = float(agent.get("approach_height", 0.08))
        self.lock_min_hits = int(agent.get("lock_min_hits", 3))
        self.grip_stall_threshold = int(agent.get("grip_stall_threshold", 8))

        # ---------- 图像误差驱动视觉伺服 ----------
        # ★ 为什么不靠标定的绝对坐标：标定的平移量在现场会漂 —— 实测把底座正前方
        #   的袋子换算成了"臂座后方 8cm"的不可达点，闭环第一次 move_to 直接被 IK
        #   拒掉（3 次尝试全在 1 秒内失败）。相机装在夹爪上，所以「把目标的
        #   grasp_point 推到画面目标像素」等价于「夹爪对准目标」：只借用标定的
        #   线性部分当雅可比（粗尺度），绝对位置由迭代收敛，不再依赖会漂的平移量。
        tpx = agent.get("servo_target_px") or [320.0, 240.0]
        self.servo_target_px = (float(tpx[0]), float(tpx[1]))
        self.servo_damping = float(agent.get("servo_damping", 0.25))
        self.servo_step_limit = float(agent.get("servo_step_limit", 0.04))
        self.servo_max_iters = int(agent.get("servo_max_iters", 12))
        self.servo_tol_px = float(agent.get("servo_tol_px", 12.0))
        #: 跟踪跳变门限（px）。伺服每步只走几厘米，目标的像素位移有限 —— 超过这个
        #: 距离就判"跟丢"，避免被**固定在相机上的东西**（夹爪本体也会被模型当成
        #: plastic_bag）吸走：那种目标的像素随臂运动几乎不变，会让伺服一路走到底。
        self.servo_track_gate = float(agent.get("servo_track_gate", 120.0))
        #: 伺服机位相对「接近高度」再抬高多少（m）。★ 必须抬高：低机位俯视时
        #: 相机自己的腕部/线缆就在画面里，模型会把它们也当成塑料袋（实测 conf 0.85，
        #: 而且是全场最高）—— 那种目标随臂同步移动，伺服永远收敛不了。抬高到
        #: 腕部之上，画面里就只剩地面目标了。
        self.servo_lift = float(agent.get("servo_lift", 0.15))
        #: 响应性判据的下限：预期像素位移小于这个值时不做判定（避免与噪声比大小）。
        self.servo_min_expected_px = float(agent.get("servo_min_expected_px", 25.0))
        #: 抓取瞄准点：center=检测框中心（「夹最中间」）；mouth=袋口（框下沿略上）。
        self.grasp_point_mode = str(agent.get("grasp_point_mode", "center")).strip().lower()
        #: 识别前是否先把臂摆到俯视观测位（现场＝低头看水面）。识别与伺服同一机位，
        #: 画面口径一致 —— 否则会出现「在待命位锁定、到伺服位已经看不见目标」。
        self.observe_look_down = bool(agent.get("observe_look_down", True))
        fb = agent.get("servo_fallback") or [0.0, 0.10]
        self.servo_fallback = (float(fb[0]), float(fb[1]))
        box = agent.get("servo_box") or {}
        bx = box.get("x") or [-0.30, 0.30]
        by = box.get("y") or [-0.05, 0.30]
        self.servo_box = ((float(bx[0]), float(bx[1])), (float(by[0]), float(by[1])))

        # ---------- 抓取姿态 ----------
        # ★ 俯仰角口径按厂商源码核对：ROS 版 IK（armpi_fpv_kinematics）里
        #   「夹持器与水平面夹角」取值区间是 [-180, 0]，桌面/水面抓取用
        #   **-175**（warehouse/in.py、object_sorting/sorting.py 的
        #   grasps.grasp_pos.rotation.r）。原先沿用待命位 -65°，在真机上
        #   爪子是斜的、不是朝下压住目标。
        self.grasp_pitch = float(agent.get("grasp_pitch", -175))
        home = arm.get("home") or {}
        pr = home.get("pitch_range") or [-180, 0]
        self.pitch_range = (float(pr[0]), float(pr[1]))

        # ---------- 放置点（旁边的料槽）----------
        # 几何口径同样取自厂商：warehouse/in.py 的货架位 x=±0.275、y=0、
        # 底层 z=0.02、底层俯仰角 -130（R1/L1）。臂座两侧的料槽就是同一类
        # 「侧向固定工位」，所以默认值直接沿用这一组。
        place = agent.get("place") or {}
        #: 槽口上方接近高度（**绝对 z**）——不能用"水面 + 偏移"：水面在甲板
        #: 以下时那个算法会把臂送到槽口下面去。
        self.place_approach_z = float(place.get("approach_z", 0.12))
        #: 槽内落料高度（**绝对 z**，m）。0.02 与厂商货架底层同口径。
        self.place_drop_z = float(place.get("drop_z", 0.02))
        self.place_retreat_z = float(place.get("retreat_z", 0.20))
        self.place_pitch = float(place.get("pitch", -130))
        self.place_settle_s = float(place.get("settle_s", 0.5))
        self.place_move_ms = int(place.get("coord_move_ms", 900))
        self.place_drop_ms = int(place.get("drop_move_ms", 700))
        #: 配置文件里的兜底放置点（示教值存在 calibration.json 时会盖过它）
        self.config_place: dict[str, Any] | None = None
        if isinstance(place.get("coord"), (list, tuple)) and len(place.get("coord")) >= 2:
            self.config_place = {"coord": [float(place["coord"][0]), float(place["coord"][1])],
                                 "source": "config"}
        elif isinstance(place.get("pose"), dict) and place.get("pose"):
            self.config_place = {"pose": {int(k): int(v) for k, v in place["pose"].items()},
                                 "source": "config"}

        # ★ 夹爪脉宽口径按厂商源码核对：**越大越闭**（in.py 夹住 450 / 松开 75）
        self.gripper_open_pulse = int(arm.get("gripper_open", 200))
        self.gripper_close_pulse = int(arm.get("gripper_close", 450))

        self._phase = "idle"
        self._stop = False
        self._detect_rounds = max(8, int(6.0 / max(self.frame_period_s, 0.05)))

        # mock 模式下没有真机标定 —— 造一个演示映射，让整条闭环可离线跑通
        if getattr(io, "mock", False) and not getattr(calibration, "ready", False):
            self._install_demo_calibration()

    # ------------------------------------------------------------------
    def request_stop(self) -> None:
        self._stop = True

    @property
    def phase(self) -> str:
        return self._phase

    def _set_phase(self, phase: str, **info: Any) -> None:
        self._phase = phase
        if self.on_phase is not None:
            try:
                self.on_phase(phase, info)
            except Exception:  # noqa: BLE001
                logger.exception("on_phase 回调异常")

    def _install_demo_calibration(self) -> None:
        """mock 专用：装载演示标定映射，让闭环可离线跑通。"""
        from .calibration import demo_calibration

        self.cal = demo_calibration(plane_z=self.depth_offset)
        logger.info("mock 模式：已装载演示标定映射（非真标定）")

    # ------------------------------------------------------------------
    # 放置点（旁边的料槽）
    # ------------------------------------------------------------------
    def _resolve_place(self) -> dict[str, Any]:
        """取当前生效的放置点：示教值（calibration.json）> config 兜底 > 无。

        示教值优先的原因：它是操作员在**这台机器**上摆出来的；config 里的
        坐标是纸面推算，换一台机器/重标定就未必还对。
        """
        desc = describe_place(getattr(self.cal, "place", None))
        if desc["ready"]:
            return desc
        if self.config_place:
            return describe_place(self.config_place)
        return {"kind": "none", "target": None, "ready": False,
                "source": "none", "saved_at": None}

    def place_info(self) -> dict[str, Any]:
        """放置点的只读描述（状态接口与自检共用）。"""
        return self._resolve_place()

    # ------------------------------------------------------------------
    def run(self, task_id: str = "", max_retry: int | None = None) -> GraspOutcome:
        """跑完整闭环，直到抓成功或重试超限。"""
        self._stop = False
        limit = int(max_retry or self.max_retry)
        log: list[dict[str, Any]] = []

        if not getattr(self.cal, "ready", False):
            self._set_phase("error", message="未标定")
            return GraspOutcome(ok=False, phase="error",
                                message="未完成 9 点标定，无法换算抓取坐标")
        if not getattr(self.detector, "info", lambda: {})().get("ready", False):
            self._set_phase("error", message="未部署模型")
            return GraspOutcome(ok=False, phase="error",
                                message="active 目录无可用模型（best.onnx/.pt），请先训练并部署")
        if not self._resolve_place()["ready"]:
            # 没有放置点就不开工：夹起来了却没地方放，比不抓更糟
            self._set_phase("error", message="未配置料槽放置点")
            return GraspOutcome(
                ok=False, phase="error",
                message="未配置料槽放置点：请在作业台「放置点（料槽）」示教一次，"
                        "或填 config.yaml 的 agent.place.coord / agent.place.pose",
            )

        # ★ 低头观测：识别前先把臂摆到俯视观测位（现场＝低头看水面）。
        #   与伺服用同一机位，保证「锁定时看得见」= 「伺服时也看得见」。
        if self.observe_look_down:
            ox, oy = self._clamp_box(*self.servo_fallback)
            oz = self.cal.plane_z + self.approach_height + self.servo_lift
            if not self.io.move_to(ox, oy, oz, pitch=self.grasp_pitch,
                                   pitch_range=self.pitch_range):
                logger.warning("观测位 (%+.2f,%+.2f,%.3f) 不可达，按当前姿态继续识别",
                               ox, oy, oz)
            time.sleep(max(0.4, self.frame_period_s * 2))

        for attempt in range(1, limit + 1):
            if self._stop:
                break
            entry: dict[str, Any] = {"attempt": attempt}

            lock = self._detect_and_lock(task_id, log)
            if lock is None:
                entry["result"] = "no_target"
                log.append(entry)
                continue
            entry["lock"] = lock.to_dict()

            servo = self._servo_approach(lock, log)
            if servo is None:
                entry["result"] = "servo_failed"
                log.append(entry)
                continue

            grip_pulse = self._grasp(servo[0], servo[1], log)
            target_gone, grip_stalled = self._verify(lock, grip_pulse, log)
            entry.update({"grip_pulse": grip_pulse, "target_gone": target_gone,
                          "grip_stalled": grip_stalled})

            # ★「原位消失」这条证据只在**有真实执行端**时才是硬证据。执行端是
            #   mock（开发机无真机，舵机只是模拟回读）时，物理上根本没有东西把
            #   目标移走，该证据不可得 —— 此时自动降级为单证据（夹爪停滞），并在
            #   日志里标明原因。真机（io.mock=False）行为一字未改，仍要求双证据。
            require_gone = not bool(getattr(self.io, "mock", False))
            verified = grip_stalled and (target_gone or not require_gone)

            if not verified:
                # 证据不齐 → 视为不确定，回检测重试（并放下已夹物，避免叠压）
                self.io.gripper_open()
                entry["result"] = "verify_failed"
                log.append(entry)
                continue
            entry["verify"] = ("双证据（原位消失 + 夹爪停滞）" if target_gone
                               else "单证据（mock 执行端：原位消失不可得，仅夹爪停滞）")

            placed = self._place(log)
            entry["result"] = "ok" if placed else "placed_failed"
            entry["placed"] = placed
            log.append(entry)
            self._set_phase("done", attempt=attempt, placed=placed)
            message = (f"抓取成功并放入料槽（第 {attempt} 次尝试）" if placed
                       else f"抓取成功但未放入料槽（第 {attempt} 次尝试）")
            return GraspOutcome(ok=True, phase="done", attempts=attempt,
                                detected=len(log), grip_pulse=grip_pulse,
                                target_gone=target_gone, grip_stalled=grip_stalled,
                                placed=placed,
                                message=message, log=log)

        message = "已停止" if self._stop else f"重试 {limit} 次仍未成功"
        self._set_phase("error", message=message)
        return GraspOutcome(ok=False, phase="error", attempts=limit,
                            detected=len(log), message=message, log=log)

    # ------------------------------------------------------------------
    # ①② 检测与锁定
    # ------------------------------------------------------------------
    def _detect_and_lock(self, task_id: str, log: list[dict[str, Any]]) -> Any:
        self._set_phase("detecting", task_id=task_id)
        hits: dict[int, int] = {}
        best: Any = None
        for _ in range(self._detect_rounds):
            if self._stop:
                return None
            frame = self.io.latest_frame()
            cands = self._candidates(frame)
            if cands:
                self._set_phase("locking", count=len(cands))
                top = max(cands, key=lambda d: d.width * d.height)
                key = self._track_key(top)
                hits[key] = hits.get(key, 0) + 1
                if hits[key] >= self.lock_min_hits:
                    best = top
                    break
            time.sleep(self.frame_period_s)
        return best

    def _candidates(self, frame: Any) -> list[Any]:
        """决策层过滤：目标类候选 + HSV 辅助校验。

        HSV 只作**辅证**，不作主判据 —— 水面反光会破坏颜色稳定性，
        主判据始终是模型类别与置信度。这里只剔除「模型说是袋、但颜色
        几乎不可能是袋」的离群点（例如高饱和的彩色浮标被误判成袋）。
        """
        if frame is None:
            return []
        targets = self.detector.targets(frame)
        kept: list[Any] = []
        for det in targets:
            if self._color_plausible(frame, det):
                kept.append(det)
        return kept

    def _color_plausible(self, frame: Any, det: Any) -> bool:
        try:
            import cv2
            import numpy as np
        except ImportError:
            return True
        u, v = det.center
        x1, y1 = int(max(0, det.x1)), int(max(0, det.y1))
        x2, y2 = int(min(frame.shape[1], det.x2)), int(min(frame.shape[0], det.y2))
        if x2 <= x1 or y2 <= y1:
            return True
        patch = frame[y1:y2, x1:x2]
        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        sat = float(np.mean(hsv[:, :, 1]))
        # 塑料袋低饱和（白/半透明），浮标高饱和。阈值放宽到 120，
        # 只当"明显不可能"时才否决，避免误杀脏污的袋子。
        return sat <= 120.0

    @staticmethod
    def _track_key(det: Any) -> int:
        """粗糙的轨迹键：按检测框中心量化到 40px 网格。

        目的是去抖 —— 同一位置连续命中若干帧才算锁定，避免单帧误检
        就触发抓取。真正复杂的 SORT 跟踪不必要：场景里只有少数目标。
        """
        u, v = det.center
        return int(u // 40) * 1000 + int(v // 40)

    # ------------------------------------------------------------------
    # ③ 视觉伺服逼近
    # ------------------------------------------------------------------
    def _servo_approach(self, lock: Any, log: list[dict[str, Any]]) -> tuple[float, float] | None:
        """把锁定目标的 grasp_point 推到画面目标像素，返回收敛后的臂位 (x, y)。

        与「用标定绝对坐标算目标点」的本质区别：这里只把标定的**线性部分**当
        雅可比（粗尺度），绝对位置靠逐帧迭代收敛。所以标定的平移量漂了也不影响
        —— 像素偏差直接指示该往哪走多少，这正是「相机装在夹爪上」的用法。
        """
        self._set_phase("servoing")
        z = self.cal.plane_z + self.approach_height + self.servo_lift

        try:
            wx, wy, _ = self.cal.pixel_to_world(*self._aim_point(lock))
        except Exception as exc:  # noqa: BLE001
            logger.warning("坐标换算失败，改用兜底起点: %s", exc)
            wx, wy = self.servo_fallback
        # ★ 不再用 work_radius 闸门：它依赖标定的绝对坐标，而那份标定本身就不可信
        #   （实测把底座正前方的袋子算成"臂座后方 8cm"，闸门反而会把真目标拒掉）。
        #   伺服的唯一安全边界是 servo_box —— 不管标定说什么，臂都不会跑出那个盒。
        # ★ 起点用**配置的搜索起点**（现场＝底座正前方），不用标定估计：
        #   标定的平移量本身就不可信（实测偏 16cm），而低机位视野只有约 ±5cm ——
        #   从偏了 16cm 的估计点起步会直接看不到目标。标定估计只作兜底。
        x, y = self._clamp_box(*self.servo_fallback)
        if not self.io.move_to(x, y, z, pitch=self.grasp_pitch, pitch_range=self.pitch_range):
            ex, ey = self._clamp_box(wx, wy)
            logger.info("搜索起点不可达，退回标定估计 (%+.3f, %+.3f)", ex, ey)
            if not self.io.move_to(ex, ey, z, pitch=self.grasp_pitch,
                                   pitch_range=self.pitch_range):
                logger.warning("起始点都不可达（搜索起点与标定估计）")
                return None
            x, y = ex, ey
        time.sleep(max(0.4, self.frame_period_s * 2))

        j = self.cal.pixel_jacobian()          # d(world)/d(pixel) 的 2x2 线性部分
        det_j = j[0][0] * j[1][1] - j[0][1] * j[1][0]
        jinv = (None if abs(det_j) < 1e-12 else
                ((j[1][1] / det_j, -j[0][1] / det_j), (-j[1][0] / det_j, j[0][0] / det_j)))
        prev_px: tuple[float, float] | None = None
        prev_step: tuple[float, float] | None = None
        for k in range(self.servo_max_iters):
            if self._stop:
                return None
            # 第 0 轮以锁定位置为参考；之后一律以**上一帧的像素**为参考并加门限，
            # 避免被固定在相机上的东西（夹爪本体也被模型当成塑料袋）吸走。
            frame = self.io.latest_frame()
            det = (self._nearest_target(frame, lock) if prev_px is None
                   else self._nearest_target(frame, prev_px, gate=self.servo_track_gate))
            if det is None:
                logger.info("伺服第 %d 轮跟丢目标，回检测", k)
                return None
            u, v = self._aim_point(det)
            # ★ 响应性判据：上一步臂确实移动了，但目标的像素没按预期移动 —— 说明
            #   这个目标在被相机"带着走"（臂体/线缆的误检）。继续伺服只会一路撞到
            #   工作盒边界然后抓空，所以这里明确放弃，让上层重新检测换目标。
            if prev_px is not None and prev_step is not None and jinv is not None:
                px, py = prev_step
                exp_u = -(jinv[0][0] * px + jinv[0][1] * py)
                exp_v = -(jinv[1][0] * px + jinv[1][1] * py)
                exp_mag = (exp_u * exp_u + exp_v * exp_v) ** 0.5
                obs_mag = ((u - prev_px[0]) ** 2 + (v - prev_px[1]) ** 2) ** 0.5
                if exp_mag > self.servo_min_expected_px and obs_mag < 0.25 * exp_mag:
                    logger.warning(
                        "伺服第 %d 轮：臂移动 %.3fm，目标像素却只动 %.0fpx（预期 %.0fpx）"
                        "→ 疑似固定在相机上的误检，放弃本目标",
                        k, (px * px + py * py) ** 0.5, obs_mag, exp_mag)
                    log.append({"servo_rejected": "camera_fixed_target",
                                "observed_px": round(obs_mag, 1),
                                "expected_px": round(exp_mag, 1)})
                    return None
            prev_px = (u, v)
            eu = self.servo_target_px[0] - u       # 目标像素 - 当前像素
            ev = self.servo_target_px[1] - v
            if abs(eu) <= self.servo_tol_px and abs(ev) <= self.servo_tol_px:
                log.append({"servo_converged": True,
                            "residual_px": round(abs(eu) + abs(ev), 1),
                            "arm_xy": [round(x, 4), round(y, 4)]})
                return x, y
            # Δ臂 = -J · 像素误差（J 为 d(world)/d(pixel)）；阻尼 + 限幅保证收敛
            logger.info("伺服第 %d 轮：像素误差 (%+.0f, %+.0f) 臂位 (%+.3f, %+.3f)",
                        k, eu, ev, x, y)
            dx = -(j[0][0] * eu + j[0][1] * ev) * self.servo_damping
            dy = -(j[1][0] * eu + j[1][1] * ev) * self.servo_damping
            dx = max(-self.servo_step_limit, min(self.servo_step_limit, dx))
            dy = max(-self.servo_step_limit, min(self.servo_step_limit, dy))
            prev_step = (dx, dy)
            x, y = self._clamp_box(x + dx, y + dy)
            if not self.io.move_to(x, y, z, pitch=self.grasp_pitch,
                                   pitch_range=self.pitch_range):
                logger.info("伺服第 %d 轮不可达 (%+.3f, %+.3f)", k, x, y)
                return None
            time.sleep(self.frame_period_s)
        log.append({"servo_converged": False, "iters": self.servo_max_iters,
                    "arm_xy": [round(x, 4), round(y, 4)]})
        return x, y      # 未收敛也用最后位置继续（抓取验证会兜底）

    def _nearest_target(self, frame: Any, ref: Any, gate: float | None = None) -> Any:
        """在当前帧里找离参考点最近的目标（邻近跟踪）。

        ``ref`` 可以是 ``(u, v)`` 或带 ``.center`` 的检测对象。给了 ``gate`` 就要求
        距离不超过它 —— 否则判「跟丢」，避免被画面里**固定在相机上**的东西吸走。
        """
        if frame is None:
            return None
        cands = self._candidates(frame)
        if not cands:
            return None
        if isinstance(ref, (tuple, list)):
            ru, rv = float(ref[0]), float(ref[1])
        else:
            ru, rv = ref.center
        best = min(cands, key=lambda d: (d.center[0] - ru) ** 2 + (d.center[1] - rv) ** 2)
        if gate is not None:
            dist = ((best.center[0] - ru) ** 2 + (best.center[1] - rv) ** 2) ** 0.5
            if dist > float(gate):
                return None
        return best

    def _clamp_box(self, x: float, y: float) -> tuple[float, float]:
        """把臂位夹进伺服工作盒。

        伺服不再依赖标定的绝对坐标，所以安全性由这个盒约束兜住 ——
        保证机械臂不会为了追一个误检而跑出工作区。
        """
        (x0, x1), (y0, y1) = self.servo_box
        return (min(max(x, x0), x1), min(max(y, y0), y1))

    def _aim_point(self, det: Any) -> tuple[float, float]:
        """抓取瞄准点：``center`` 用检测框中心（「夹最中间」），``mouth`` 用袋口。"""
        if self.grasp_point_mode == "center":
            return det.center
        return det.grasp_point

    # ------------------------------------------------------------------
    # ④ 精准夹取
    # ------------------------------------------------------------------
    def _grasp(self, x: float, y: float, log: list[dict[str, Any]]) -> int:
        """在伺服收敛到的 (x, y) 正上方下探夹取（IK 现算，非录制位）。"""
        self._set_phase("grasping")
        z = self.cal.plane_z + self.depth_offset
        self.io.gripper_open()
        time.sleep(0.2)
        # 下探到抓取高度
        if not self.io.move_to(x, y, z, pitch=self.grasp_pitch,
                               pitch_range=self.pitch_range, duration_ms=700):
            logger.warning("下探不可达，按当前位置夹取")
        time.sleep(0.5)
        # 自适应渐闭：夹到东西就停，不靠固定脉宽
        pulse = self.io.gripper_close_adaptive(self.grip_stall_threshold)
        time.sleep(0.3)
        # 抬起
        self.io.move_to(x, y, self.cal.plane_z + self.approach_height,
                        pitch=self.grasp_pitch, pitch_range=self.pitch_range,
                        duration_ms=700)
        log.append({"grasp_point_world": [round(x, 4), round(y, 4)], "grip_pulse": pulse})
        return int(pulse)

    # ------------------------------------------------------------------
    # ⑤ 抓取验证（双证据）
    # ------------------------------------------------------------------
    def _verify(self, lock: Any, grip_pulse: int, log: list[dict[str, Any]]) -> tuple[bool, bool]:
        self._set_phase("verifying")
        time.sleep(self.frame_period_s)
        frame = self.io.latest_frame()
        # 证据一：原位置目标消失
        lu, lv = lock.center
        cands = self._candidates(frame)
        target_gone = all(((d.center[0] - lu) ** 2 + (d.center[1] - lv) ** 2) ** 0.5 > 60
                          for d in cands)
        # 证据二：夹爪未闭到底（位置停滞）→ 说明夹住了东西
        #   ★ 方向无关判据：只看「离全闭位还差多少」。夹爪脉宽越大越闭
        #     （厂商 in.py：夹住 450 / 松开 75），所以"停在半途"就是夹到了东西。
        grip_stalled = abs(int(grip_pulse) - self.gripper_close_pulse) > self.grip_stall_threshold
        log.append({"target_gone": target_gone, "grip_stalled": grip_stalled})
        return target_gone, grip_stalled

    # ------------------------------------------------------------------
    # ⑥ 放置入槽（臂座旁边的料槽 side_hopper）
    # ------------------------------------------------------------------
    def _place(self, log: list[dict[str, Any]]) -> bool:
        """把夹住的袋子放进旁边的料槽，返回**是否真的放进去了**。

        与抓取的区别：抓取点每帧由视觉现算，放置点是固定工位 —— 所以这里
        不重新检测目标，只是把臂移到示教/配置好的槽口位置，松爪，落料。
        动作序列（coord 模式，高度一律用**绝对 z**）：

            抬到槽口上方（``place.approach_z``）
              → 下探进槽（``place.drop_z``）→ 松爪 → 停顿落料 → 抬离
              → 回待命位

        几何口径抄自厂商 `warehouse/in.py` 的侧向货架：x=±0.275、y=0、
        底层 z=0.02、俯仰角 -130（``place.pitch``）。臂座旁的料槽属于同一类
        「侧向固定工位」，所以默认值直接沿用。

        ``pose`` 模式（示教位姿）直接回放该组舵机脉宽，再走同样的松爪/落料/回位。
        两种模式下**只要动作没真正到位就返回 False** —— 上层据此区分
        「抓到了」与「抓到了且放进槽了」。
        """
        self._set_phase("placing")
        place = self._resolve_place()
        kind, target, source = place["kind"], place["target"], place["source"]
        if not place["ready"]:
            log.append({"placed": False, "reason": "no_place_point"})
            return False

        reached = True
        evidence: dict[str, Any] = {"place_kind": kind, "place_source": source}

        if kind == "coord":
            px, py = float(target[0]), float(target[1])
            up = self.io.move_to(px, py, self.place_approach_z,
                                 pitch=self.place_pitch, pitch_range=self.pitch_range,
                                 duration_ms=self.place_move_ms)
            time.sleep(max(0.3, self.frame_period_s))
            down = self.io.move_to(px, py, self.place_drop_z,
                                   pitch=self.place_pitch, pitch_range=self.pitch_range,
                                   duration_ms=self.place_drop_ms)
            reached = bool(up and down)
            evidence["place_coord"] = [round(px, 4), round(py, 4)]
            evidence["place_z"] = round(self.place_drop_z, 4)
            evidence["place_pitch"] = self.place_pitch
            if not reached:
                logger.warning("料槽放置点不可达 (%s, %s, z=%s)：就地松爪并如实上报",
                               px, py, self.place_drop_z)
        else:
            pairs = sorted((int(k), int(v)) for k, v in dict(target).items())
            evidence["place_pulses"] = pairs
            try:
                self.io.set_servos(self.place_drop_ms, pairs)
            except Exception as exc:  # noqa: BLE001 —— 链路问题不该吞掉
                logger.warning("示教放置位姿下发失败：%s", exc)
                reached = False
            time.sleep(max(0.3, self.frame_period_s))

        time.sleep(0.4)
        self.io.gripper_open()
        # 停顿：让袋子真的落进槽里再抬臂，否则会挂在夹爪上被带走
        time.sleep(max(0.0, self.place_settle_s))
        if kind == "coord":
            self.io.move_to(px, py, self.place_retreat_z,
                            pitch=self.place_pitch, pitch_range=self.pitch_range,
                            duration_ms=self.place_drop_ms)
        self.io.home()

        evidence["placed"] = bool(reached)
        log.append(evidence)
        return bool(reached)
