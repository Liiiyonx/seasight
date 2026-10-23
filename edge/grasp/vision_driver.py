"""视觉抓取驱动：把 ``GraspLoop`` 包成 ``ArmDriver``，接入现有桥接层。

注册名 ``oceanus_vision``。平台侧 MQTT 契约、桥接层、状态机**零改动** ——
只把 ``driver.backend`` 换成它。这是「执行端可替换层」的又一次落地：
上一次是从"直连串口"换成"ROS action"，这一次是换成"视觉闭环"。

与 ``HiwonderBusServoArmDriver`` 的根本差别：

======================  ==============================  ============================
                        厂商总线舵机驱动（示教回放）      本驱动（视觉闭环）
======================  ==============================  ============================
动作来源                 录制好的 pick_sequence 脉宽序列   感知 → IK 现算 → 下发
对目标敏感度             换位置就抓空                      目标进视野即可
浮标干扰                 无法区分                          三层防线（训练/推理/决策）
抓取验证                 无                                原位消失 + 夹爪停滞双证据
失败处理                 无                                重试 → 超限上报异常
舵机回读                 读电压/温度/脉宽                  读电压/位置/误差（+关节角）
======================  ==============================  ============================

ROS 依赖全部**惰性导入**（在 ``ArmIo`` 里），因此本模块在没有 rospy 的
开发机上也能被 import —— 从而 ``build_arm_driver`` 的惰性加载与其余驱动
的测试都不受影响。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EDGE = HERE.parent
if str(EDGE) not in sys.path:
    sys.path.insert(0, str(EDGE))

from arm_bridge.drivers import (  # noqa: E402
    ArmStatus,
    DeviceMode,
    DriverError,
    PickResult,
    Position,
    register_arm_driver,
)

from .arm_io import ArmIo, ArmIoError  # noqa: E402
from .calibration import Calibration, config_plane_z, load_calibration  # noqa: E402
from .config import load_config  # noqa: E402
from .grasp_loop import GraspLoop, GraspLoopError  # noqa: E402
from .vision.detector import Detector  # noqa: E402

logger = logging.getLogger("oceanus.grasp.driver")

DEFAULT_HOME_LNG = 119.6540
DEFAULT_HOME_LAT = 26.3870


class OceanusVisionArmDriver:
    """视觉闭环抓取驱动（实现 ``ArmDriver`` 协议）。"""

    def __init__(
        self,
        config_path: str | None = None,
        calibration_path: str | None = None,
        mock: bool = False,
        collected_weight: float = 0.05,
        review_result: str = "recheck",
        battery: int = 100,
        bins: dict[str, float] | None = None,
        home: dict[str, float] | None = None,
        usb_camera: int | None = None,
        **_: Any,
    ) -> None:
        self.cfg = load_config(config_path)
        self.mock = bool(mock)
        self.calibration_path = calibration_path
        # 帧源覆盖：配了就走 USB 摄像头（与作业台 --camera 同口径）
        if usb_camera is not None:
            self.cfg.setdefault("arm", {})["usb_camera"] = int(usb_camera)

        self.io = ArmIo(self.cfg, mock=self.mock)
        # ★ 有真实帧源（USB 摄像头）时用**真模型**检测：合成检测（HSV）是为合成
        #   画面设计的，喂真摄像头画面会产出一堆垃圾框。与 web/server.py 同一条
        #   口径 —— 平台派单（本驱动）与作业台两个入口行为一致。
        self.detector = Detector(self.cfg, mock=(self.mock and self.io.usb_camera is None))
        # 水面/台面高度：标定文件优先，其次 config 的 arm.plane_z
        self.cal: Calibration = load_calibration(calibration_path,
                                                 plane_z=config_plane_z(self.cfg))
        self.loop = GraspLoop(self.io, self.detector, self.cal, self.cfg,
                              on_phase=self._on_phase)

        self.collected_weight = float(collected_weight)
        self.review_result = str(review_result)
        self.battery = int(battery)
        self.bins: dict[str, float] = dict(
            bins or {"foam": 0.0, "plastic": 0.0, "mixed": 0.0}
        )
        home = home or {}
        self.home = Position(float(home.get("lng", DEFAULT_HOME_LNG)),
                             float(home.get("lat", DEFAULT_HOME_LAT)))

        self._mode = DeviceMode.IDLE
        self._started = False
        self._phase = "idle"
        self.last_outcome: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self.io.start()
            backend = self.detector.load()
            logger.info("视觉驱动就绪：检测后端=%s 标定=%s mock=%s",
                        backend, self.cal.ready, self.mock)
        except ArmIoError as exc:
            # 硬件链路起不来时不要吞掉 —— 让上层看到明确原因
            raise DriverError(str(exc)) from exc

    def _on_phase(self, phase: str, info: dict[str, Any]) -> None:
        self._phase = phase
        logger.info("抓取相位 %s %s", phase, info or {})

    # ------------------------------------------------------------------
    # ArmDriver 协议
    # ------------------------------------------------------------------
    def status(self) -> ArmStatus:
        """状态快照：舵机遥测来自真实回读（厂商 servo_states 话题）。

        ★ 这是「机械臂真的动了」的硬证据 —— 桥接层会把它塞进 telemetry
          报文的 ``servos`` 字段，平台控制台据此展示电压/位置。
        """
        servos: dict[str, Any] = {}
        try:
            self._ensure_started()
            servos = self.io.servo_telemetry()
        except DriverError:
            servos = {}
        return ArmStatus(
            mode=self._mode,
            battery=self.battery,
            bins=dict(self.bins),
            location=self.home,
            servos=servos,
        )

    def pick(self, task_id: str, target: Position, priority: int) -> PickResult:
        """执行一次视觉闭环抓取。

        成功返回 ``PickResult(ok=True)``；失败**抛 DriverError** ——
        桥接层会把 DriverError 记为 pick_failed 并以 review_result=recheck
        上报，平台侧就能看到「这次作业没成功」而不是假装成功。
        """
        self._ensure_started()
        self._mode = DeviceMode.COLLECTING
        logger.info("开始视觉抓取 task=%s target=%s priority=%s",
                    task_id, target.to_dict(), priority)
        try:
            outcome = self.loop.run(task_id=task_id)
        except GraspLoopError as exc:
            self._mode = DeviceMode.IDLE
            raise DriverError(str(exc)) from exc
        except ArmIoError as exc:
            self._mode = DeviceMode.IDLE
            raise DriverError(str(exc)) from exc

        self.last_outcome = outcome.to_dict()
        self._mode = DeviceMode.IDLE
        if not outcome.ok:
            raise DriverError(f"视觉抓取未成功：{outcome.message}")
        placed = bool(outcome.placed)
        if not placed:
            # 夹住了却没送进料槽（例如放置点不可达）：任务本身算完成，
            # 但必须留下明确痕迹 —— 人手还得去把夹爪上那袋取下来。
            logger.warning("抓取成功但未放入料槽：%s（place=%s）",
                           outcome.message, self.loop.place_info())

        self.bins["plastic"] = min(1.0, self.bins["plastic"] + self.collected_weight)
        return PickResult(
            ok=True,
            collected_weight=self.collected_weight,
            # 没进槽就报 recheck：平台侧据此知道这单需要人工复核
            review_result=self.review_result if placed else "recheck",
            bins_after=dict(self.bins),
        )

    def pause(self) -> None:
        self.loop.request_stop()
        self._mode = DeviceMode.PAUSED

    def resume(self) -> None:
        self._mode = (DeviceMode.NAVIGATING
                      if self._mode == DeviceMode.PAUSED else DeviceMode.IDLE)

    def return_home(self) -> None:
        self._ensure_started()
        try:
            self.io.home()
        except ArmIoError as exc:
            raise DriverError(f"回待命位失败：{exc}") from exc
        self._mode = DeviceMode.IDLE

    def emergency_stop(self) -> None:
        """急停：停止闭环并松开夹爪（而不是"发 home"继续动）。"""
        self.loop.request_stop()
        try:
            self.io.gripper_open()
        except ArmIoError:
            pass
        self._mode = DeviceMode.E_STOP

    # ------------------------------------------------------------------
    # 供作业台页面复用的只读接口
    # ------------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        return {
            "mock": self.mock,
            "phase": self._phase,
            "mode": self._mode,
            "calibration_ready": bool(self.cal.ready),
            "calibration_rmse": self.cal.rmse,
            "calibration_points": len(self.cal.pairs),
            # 放置点（料槽）：派单驱动的这套闭环里，"放哪儿"是硬前置
            "place": self.loop.place_info(),
            "detector": self.detector.info(),
            "last_outcome": self.last_outcome,
        }


# 惰性注册：只有 import 本模块才进注册表，避免污染其它驱动的测试环境。
register_arm_driver("oceanus_vision", OceanusVisionArmDriver)

__all__ = ["OceanusVisionArmDriver"]
