"""Arm driver adapters.

``ArmDriver`` is the only interface a new arm has to implement. The bridge and
the platform MQTT contract stay unchanged when hardware changes.

Evidence level: E1/E2. Simulated and generic HTTP drivers are deterministic
prototype adapters; they do not prove physical pickup or field acceptance.

Python 3.6 note
---------------
本文件要能在树莓派（ArmPi FPV 出厂镜像，**Python 3.6.9**）上直接 import，
所以：

* **不写** ``from __future__ import annotations``（3.6 不支持）
* 类型注解一律用 ``Dict[...]``/``Optional[...]``，**不用** ``Dict[...]``
  这种下标泛型（3.6 定义时求值即失败）
* ``dataclass`` / ``Protocol`` 从 :mod:`arm_bridge.py36_compat` 取
"""

import importlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Type

from device_sim.protocol import DeviceMode, Position

# ★ 3.6 兼容：从 py36_compat 取，而不是标准库
from .py36_compat import (
    DictStrAny,
    DictStrFloat,
    OptStr,
    Protocol,
    field,
    make_dataclass,
)

_BINS_DEFAULT = field(
    default_factory=lambda: {"foam": 0.0, "plastic": 0.0, "mixed": 0.0}
)


class DriverError(RuntimeError):
    """Raised when the underlying arm SDK rejects or fails an action."""


# ★ 原来用 @dataclass(frozen=True) class 语法，但 3.6 的 __annotations__
#   是普通 dict、不保证字段顺序，而本项目的构造调用有位置参数
#   （页面与 MQTT 出口都在按序传），所以改成显式列出字段顺序 ——
#   行为等价，顺序确定。
PickResult = make_dataclass(
    "PickResult",
    [
        ("ok", True),
        ("collected_weight", 0.0),
        ("review_result", "confirmed"),
        ("evidence_url", None),
        ("bins_after", _BINS_DEFAULT),
    ],
    doc="Outcome of one pickup action, translated to the platform contract.",
)

#: Current status snapshot reported as marine telemetry.
#:
#: ``servos`` 是逐舵机遥测（电压/温度/位置）—— ★ 证据链价值：
#: 这些数字来自舵机本身，是"机械臂真的动了"的硬证据，
#: 比 progress 报文自报更有说服力。None 表示该次读取失败。
ArmStatus = make_dataclass(
    "ArmStatus",
    [
        ("mode", DeviceMode.IDLE),
        ("battery", 100),
        ("bins", _BINS_DEFAULT),
        ("location", Position(119.6540, 26.3870)),
        ("heading", 0.0),
        ("speed", 0.0),
        ("servos", field(default_factory=dict)),
    ],
    doc="Current status snapshot reported as marine telemetry.",
)


class ArmDriver(Protocol):
    """Adapter contract for an arbitrary arm SDK.

    Implement this class once per physical arm model. The bridge calls these
    methods synchronously and translates results into MQTT messages.
    """

    def status(self) -> ArmStatus: ...

    def pick(self, task_id: str, target: Position, priority: int) -> PickResult: ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def return_home(self) -> None: ...

    def emergency_stop(self) -> None: ...


#: 需要额外运行时依赖的驱动 -> 模块路径。惰性加载，见 build_arm_driver。
_OPTIONAL_DRIVER_MODULES = {
    "ros_arm_control": "arm_bridge.ros_driver",
}

ArmDriverFactory = Callable[..., Any]

ARM_DRIVER_REGISTRY: Dict[str, ArmDriverFactory] = {}


def register_arm_driver(name: str, factory: ArmDriverFactory) -> None:
    """Register a named arm driver so config can select it without code changes."""
    driver_name = str(name).strip()
    if not driver_name:
        raise ValueError("driver name must not be empty")
    ARM_DRIVER_REGISTRY[driver_name] = factory


def build_arm_driver(
    driver_cfg: Dict[str, Any],
    backend: Optional[str] = None,
) -> Any:
    """Build a driver from the ``driver`` config section.

    Supported layouts:

    * ``backend: simulated`` -> SimulatedArmDriver
    * ``backend: http`` -> HttpArmDriver
    * ``backend: <brand>`` with a ``<brand>: {...}`` block -> registered class
    * ``backend: <brand>`` with ``vendors.<brand>: {kind, ...}`` -> registered
      class selected by ``kind``
    """
    effective_backend = str(backend or driver_cfg.get("backend", "simulated"))

    # ★ 可选驱动惰性加载：ros_driver 只在真正选用它时才 import。
    #   原因：它 import rospy，而 rospy 只在树莓派上有；开发机与 CI 上
    #   import 会失败。同时它靠 import 副作用把自己注册进本表，
    #   不import 就查不到 —— 所以在查表**之前**先尝试加载。
    if effective_backend in _OPTIONAL_DRIVER_MODULES:
        mod = _OPTIONAL_DRIVER_MODULES[effective_backend]
        if mod not in sys.modules:
            try:
                importlib.import_module(mod)
            except ImportError as exc:  # pragma: no cover - 取决于环境
                raise DriverError(
                    f"arm driver {effective_backend!r} requires module {mod!r}: {exc}"
                ) from exc

    vendors = driver_cfg.get("vendors")
    if isinstance(vendors, dict) and effective_backend in vendors:
        vendor = vendors[effective_backend]
        if not isinstance(vendor, dict):
            raise ValueError(f"vendor {effective_backend!r} must be a mapping")
        kind = str(vendor.get("kind", "http"))
        factory = ARM_DRIVER_REGISTRY.get(kind)
        if factory is None:
            raise ValueError(
                f"unknown driver kind {kind!r}; registered: "
                f"{sorted(ARM_DRIVER_REGISTRY)}"
            )
        params = {key: value for key, value in vendor.items() if key != "kind"}
        return factory(**params)

    factory = ARM_DRIVER_REGISTRY.get(effective_backend)
    if factory is None:
        raise ValueError(
            f"unknown arm driver backend {effective_backend!r}; registered: "
            f"{sorted(ARM_DRIVER_REGISTRY)}"
        )
    params = driver_cfg.get(effective_backend)
    if not isinstance(params, dict):
        params = {}
    return factory(**params)

class SimulatedArmDriver:
    """Deterministic arm driver for no-hardware self tests.

    It performs no physical action. It exists so the full
    ``task -> ack -> progress -> telemetry`` loop can be verified before a real
    arm arrives.
    """

    def __init__(
        self,
        battery: int = 100,
        bins: Optional[Dict[str, float]] = None,
        home: Optional[Dict[str, float]] = None,
        collected_weight: float = 0.05,
        review_result: str = "confirmed",
    ) -> None:
        self.battery = int(battery)
        self.bins: Dict[str, float] = dict(
            bins or {"foam": 0.0, "plastic": 0.0, "mixed": 0.0}
        )
        home = home or {"lng": 119.6540, "lat": 26.3870}
        self.home = Position(float(home["lng"]), float(home["lat"]))
        self.mode = DeviceMode.IDLE
        self.collected_weight = float(collected_weight)
        self.review_result = review_result
        self.pick_calls = 0

    def status(self) -> ArmStatus:
        return ArmStatus(
            mode=self.mode,
            battery=self.battery,
            bins=dict(self.bins),
            location=self.home,
        )

    def pick(self, task_id: str, target: Position, priority: int) -> PickResult:
        self.pick_calls += 1
        self.mode = DeviceMode.IDLE
        self.bins["foam"] = min(
            1.0, self.bins["foam"] + self.collected_weight * 2.0
        )
        return PickResult(
            ok=True,
            collected_weight=self.collected_weight,
            review_result=self.review_result,
            bins_after=dict(self.bins),
        )

    def pause(self) -> None:
        self.mode = DeviceMode.PAUSED

    def resume(self) -> None:
        self.mode = DeviceMode.NAVIGATING if self.mode == DeviceMode.PAUSED else DeviceMode.IDLE

    def return_home(self) -> None:
        self.mode = DeviceMode.IDLE

    def emergency_stop(self) -> None:
        self.mode = DeviceMode.E_STOP


class HttpArmDriver:
    """Generic HTTP adapter example for arms that expose an HTTP SDK.

    Configure ``endpoint`` to the arm's control endpoint. Each call sends one
    JSON body with an ``action`` field; the response may carry optional
    ``mode`` / ``ok`` / ``collected_weight`` / ``review_result`` /
    ``evidence_url`` / ``bins_after`` fields.
    """

    def __init__(
        self,
        endpoint: str,
        method: str = "POST",
        timeout: float = 10.0,
        headers: Optional[Dict[str, str]] = None,
        battery: int = 100,
        bins: Optional[Dict[str, float]] = None,
        home: Optional[Dict[str, float]] = None,
    ) -> None:
        self.endpoint = str(endpoint).rstrip("/")
        self.method = str(method).upper()
        self.timeout = float(timeout)
        self.headers = {"Content-Type": "application/json"}
        self.headers.update(headers or {})
        self.battery = int(battery)
        self.bins: Dict[str, float] = dict(
            bins or {"foam": 0.0, "plastic": 0.0, "mixed": 0.0}
        )
        home = home or {"lng": 119.6540, "lat": 26.3870}
        self.home = Position(float(home["lng"]), float(home["lat"]))
        self.mode = DeviceMode.IDLE

    def status(self) -> ArmStatus:
        return ArmStatus(
            mode=self.mode,
            battery=self.battery,
            bins=dict(self.bins),
            location=self.home,
        )

    def _call(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint, data=body, method=self.method, headers=self.headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise DriverError(f"{action} failed with HTTP {exc.code}") from exc
        except OSError as exc:
            raise DriverError(f"{action} failed: {exc}") from exc
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
        self.mode = str(data.get("mode", self.mode))
        return data

    def pick(self, task_id: str, target: Position, priority: int) -> PickResult:
        data = self._call(
            "pick",
            {
                "action": "pick",
                "task_id": task_id,
                "target": target.to_dict(),
                "priority": int(priority),
            },
        )
        bins = data.get("bins_after")
        return PickResult(
            ok=bool(data.get("ok", True)),
            collected_weight=float(data.get("collected_weight", 0.0)),
            review_result=str(data.get("review_result", "confirmed")),
            evidence_url=data.get("evidence_url"),
            bins_after=dict(bins) if isinstance(bins, dict) else dict(self.bins),
        )

    def pause(self) -> None:
        self._call("pause", {"action": "pause"})

    def resume(self) -> None:
        self._call("resume", {"action": "resume"})

    def return_home(self) -> None:
        self._call("return_home", {"action": "return_home"})

    def emergency_stop(self) -> None:
        self._call("emergency_stop", {"action": "emergency_stop"})


class HiwonderBusServoArmDriver:
    """Adapter for Hiwonder Raspberry Pi bus-servo arms.

    The vendor ``ros_robot_controller_sdk.Board`` talks to the expansion board
    over the Pi UART (default ``/dev/ttyAMA0`` at 1 Mbaud). The platform's
    GPS ``target`` is intentionally not treated as joint coordinates: ``pick``
    plays a pre-taught ``pick_sequence`` (servo id / position pairs recorded
    with the vendor's teach-and-playback example), then returns the platform
    ``PickResult``.

    The SDK is imported lazily so this module stays testable and importable on
    machines without pyserial or the arm attached. Physical actions raise
    :class:`DriverError` when the SDK is unavailable; ``status`` still returns
    the configured deterministic snapshot for config self checks.
    """

    def __init__(
        self,
        serial_port: str = "/dev/ttyAMA0",
        baudrate: int = 1000000,
        timeout: float = 5.0,
        servo_ids: Optional[List[int]] = None,
        home_positions: Optional[List[List[int]]] = None,
        pick_sequence: Optional[List[Dict[str, Any]]] = None,
        pick_sequence_file: Optional[str] = None,
        pick_positions: Optional[List[List[int]]] = None,
        pick_duration: float = 1.0,
        home_duration: float = 1.5,
        collected_weight: float = 0.0,
        review_result: str = "recheck",
        evidence_url: Optional[str] = None,
        battery: int = 100,
        bins: Optional[Dict[str, float]] = None,
        home: Optional[Dict[str, float]] = None,
    ) -> None:
        self.serial_port = str(serial_port)
        self.baudrate = int(baudrate)
        self.timeout = float(timeout)
        self.servo_ids = [int(sid) for sid in (servo_ids or [1, 2, 3, 4])]
        if not self.servo_ids:
            raise ValueError("servo_ids must not be empty")
        self.home_positions = self._normalize_positions(home_positions or [])
        if pick_sequence_file is not None and pick_sequence is not None:
            raise ValueError(
                "use either pick_sequence or pick_sequence_file, not both"
            )
        if pick_sequence_file is not None:
            self.pick_sequence = self._load_sequence_file(
                str(pick_sequence_file)
            )
        else:
            self.pick_sequence = self._normalize_sequence(pick_sequence)
        if not self.pick_sequence and pick_positions:
            self.pick_sequence = [
                {
                    "duration": float(pick_duration),
                    "positions": self._normalize_positions(pick_positions),
                }
            ]
        if not self.pick_sequence:
            raise ValueError(
                "pick_sequence or pick_positions is required for a physical arm"
            )
        self.pick_duration = float(pick_duration)
        self.home_duration = float(home_duration)
        self.collected_weight = float(collected_weight)
        self.review_result = str(review_result)
        self.evidence_url = evidence_url
        self.battery = int(battery)
        self.bins: Dict[str, float] = dict(
            bins or {"foam": 0.0, "plastic": 0.0, "mixed": 0.0}
        )
        home = home or {"lng": 119.6540, "lat": 26.3870}
        self.home = Position(float(home["lng"]), float(home["lat"]))
        self._mode = DeviceMode.IDLE
        self._board: Optional[Any] = None

    @staticmethod
    def _normalize_positions(positions: Any) -> List[List[int]]:
        normalized: List[List[int]] = []
        for item in positions:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise ValueError(
                    "servo positions must be [servo_id, position] pairs"
                )
            servo_id = int(item[0])
            position = int(item[1])
            if servo_id < 0:
                raise ValueError(f"servo_id must be >= 0, got {servo_id}")
            if not 0 <= position <= 1000:
                raise ValueError(
                    f"bus servo position must be 0..1000, got {position}"
                )
            normalized.append([servo_id, position])
        return normalized

    @classmethod
    def _normalize_sequence(cls, sequence: Any) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for step in sequence or []:
            if isinstance(step, dict):
                duration = float(step["duration"])
                positions = cls._normalize_positions(step["positions"])
            elif isinstance(step, (list, tuple)) and len(step) == 2:
                duration = float(step[0])
                positions = cls._normalize_positions(step[1])
            else:
                raise ValueError(
                    "pick_sequence steps must be {duration, positions} maps "
                    "or [duration, [[servo_id, position], ...]] pairs"
                )
            if duration <= 0:
                raise ValueError(f"pick step duration must be > 0, got {duration}")
            normalized.append({"duration": duration, "positions": positions})
        return normalized

    @classmethod
    def _load_sequence_file(cls, path: str) -> List[Dict[str, Any]]:
        """Load a pick sequence JSON file written by a teach tool.

        Accepts three shapes, because the vendor tooling and ours differ:

        1. Vendor format — a flat list of single-servo positions::

               [1000, 940]

           That is what ``案例5 示教记录实现/bus_servo_record.py`` writes:
           it records one position per Enter press, for ONE servo. Converted
           here into single-servo steps so a vendor-recorded file can be
           replayed without hand-editing.

        2. Our format — a list of ``{"duration":…, "positions": [[id,pos]]}``.
        3. ``{"steps": [...]}`` wrapping either of the above.
        """
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(
                f"cannot read pick_sequence_file {path!r}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"pick_sequence_file {path!r} is not valid JSON: {exc}"
            ) from exc
        if isinstance(raw, dict) and isinstance(raw.get("steps"), list):
            raw = raw["steps"]
        if not isinstance(raw, list):
            raise ValueError(
                f"pick_sequence_file {path!r} must contain a list of steps "
                "or a {'steps': [...]} mapping"
            )
        # ★ 厂商扁平格式 [1000, 940, …] → 归一化成 step 列表。
        #   厂商示教程序一次只记一个舵机的位置（用 bus_servo_read_id() 取
        #   第一个舵机），所以这里按配置的 servo_ids[0] 还原。
        if raw and all(isinstance(x, (int, float)) for x in raw):
            return cls._normalize_sequence(
                [
                    {
                        "duration": 1.0,
                        "positions": [[1, int(round(float(x)))]],
                    }
                    for x in raw
                ]
            )
        return cls._normalize_sequence(raw)

    def _new_board(self) -> Any:
        # 先查串口在不在，再import SDK —— 顺序反过来会让现场拿到
        # "SDK unavailable" 这种指向错误的提示（真机没串口时 SDK 是有的，
        # 真正的原因是舵机走 ROS 而不是串口）。
        if not Path(self.serial_port).exists():
            import glob  # noqa: PLC0415

            tty_usb = sorted(glob.glob("/dev/ttyUSB*"))
            tty_acm = sorted(glob.glob("/dev/ttyACM*"))
            found = ", ".join(tty_usb + tty_acm) or "none"
            raise DriverError(
                f"serial port {self.serial_port} does not exist "
                f"(ttyUSB/ttyACM present: {found}). "
                "Either fix driver.serial_port in config.yaml, or — if the "
                "arm is driven over ROS instead of a UART — the platform "
                "needs a RosArmDriver implementing the same ArmDriver "
                "protocol; check `rosnode list` / `rostopic list` on the Pi. "
                "See docs/competitions/arm-hardware-probe-2026-10-05.md."
            )
        try:
            import ros_robot_controller_sdk as rrc  # noqa: PLC0415
        except ImportError as exc:
            raise DriverError(
                "ros_robot_controller_sdk is unavailable; copy the vendor "
                "ros_robot_controller_sdk.py onto the Pi next to the bridge "
                "and install pyserial"
            ) from exc
        try:
            return rrc.Board(
                device=self.serial_port,
                baudrate=self.baudrate,
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise DriverError(
                f"cannot open Hiwonder board on {self.serial_port}: {exc}"
            ) from exc

    def _get_board(self) -> Any:
        if self._board is None:
            self._board = self._new_board()
        return self._board

    def _stop_and_release(self, board: Any) -> None:
        board.bus_servo_stop(self.servo_ids)
        for servo_id in self.servo_ids:
            board.bus_servo_enable_torque(servo_id, False)

    def _read_servo_telemetry(self, board: Any) -> Dict[str, Any]:
        """逐个舵机回读电压/温度/位置。

        这是"机械臂真的动了"的硬证据 —— progress 报文是平台自报，
        而这里的数字来自舵机本身。答辩时被问"怎么证明真机执行了"，
        拿这个出来比任何架构图都有说服力。

        单个舵机读失败不影响其余（每个都独立 try），因为总线的
        回读是异步队列、单次调用常常正好赶不上回包。
        """
        out: Dict[str, Any] = {}
        for sid in self.servo_ids:
            item: Dict[str, Any] = {}
            for key, fn in (
                ("vin", board.bus_servo_read_vin),
                ("temp", board.bus_servo_read_temp),
                ("position", board.bus_servo_read_position),
            ):
                try:
                    val = fn(sid)
                    # 厂商 SDK 返回list，取第一个元素
                    if isinstance(val, (list, tuple)):
                        val = val[0] if val else None
                    if isinstance(val, (int, float)):
                        item[key] = round(float(val), 2)
                except Exception:  # noqa: BLE001
                    pass
            if item:
                out[str(sid)] = item
        return out

    def _read_board_voltage(self, board: Any, tries: int = 6) -> Optional[float]:
        """读控制板电压（mV）。

        ★ `Board.get_battery()` 是**非阻塞**的：它只从已收到的队列里取，
          队列空就直接返回 None。所以必须先 enable_reception、稍等、
          再重试几次 —— 原实现只调一次，等于永远读不到，battery 恒为
          构造时的初值。这是"声明了却没实现"的静默缺陷。
        """
        for _ in range(tries):
            try:
                raw = board.get_battery()
            except Exception:  # noqa: BLE001
                return None
            if isinstance(raw, (int, float)) and raw > 0:
                return float(raw)
            time.sleep(0.05)
        return None

    def status(self) -> ArmStatus:
        battery = self.battery
        servos: Dict[str, Any] = {}
        board = self._board
        if board is not None:
            try:
                board.enable_reception(True)
                time.sleep(0.1)
                servos = self._read_servo_telemetry(board)
                mv = self._read_board_voltage(board)
                if mv is not None:
                    # 控制板电压来自 2S 锂电（标称 7.4V，满电约 8.4V）。
                    # 这里是**粗略**换算，仅用于"电量明显偏低"的告警，
                    # 不作为精确 SoC —— 精确 SoC 需要放电曲线标定，暂无。
                    battery = max(0, min(100, round((mv - 6000.0) / 2400.0 * 100)))
            except Exception:  # noqa: BLE001
                # 读不到就保持上次的值；不能因遥测失败让整条链路挂掉
                pass
        return ArmStatus(
            mode=self._mode,
            battery=battery,
            bins=dict(self.bins),
            location=self.home,
            servos=servos,
        )

    def pick(self, task_id: str, target: Position, priority: int) -> PickResult:
        board = self._get_board()
        self._mode = DeviceMode.COLLECTING
        try:
            board.enable_reception(True)
            for servo_id in self.servo_ids:
                board.bus_servo_enable_torque(servo_id, True)
            for step in self.pick_sequence:
                board.bus_servo_set_position(
                    float(step["duration"]), step["positions"]
                )
                time.sleep(max(0.0, float(step["duration"]) + 0.2))
            board.bus_servo_stop(self.servo_ids)
        except DriverError:
            self._mode = DeviceMode.IDLE
            raise
        except Exception as exc:  # noqa: BLE001
            self._mode = DeviceMode.IDLE
            raise DriverError(f"Hiwonder pick failed: {exc}") from exc

        self.bins["foam"] = min(1.0, self.bins["foam"] + self.collected_weight)
        self._mode = DeviceMode.IDLE
        return PickResult(
            ok=True,
            collected_weight=self.collected_weight,
            review_result=self.review_result,
            evidence_url=self.evidence_url,
            bins_after=dict(self.bins),
        )

    def pause(self) -> None:
        board = self._get_board()
        try:
            board.bus_servo_stop(self.servo_ids)
        except Exception as exc:  # noqa: BLE001
            raise DriverError(f"Hiwonder pause failed: {exc}") from exc
        self._mode = DeviceMode.PAUSED

    def resume(self) -> None:
        self._mode = (
            DeviceMode.NAVIGATING
            if self._mode == DeviceMode.PAUSED
            else DeviceMode.IDLE
        )

    def return_home(self) -> None:
        board = self._get_board()
        try:
            if self.home_positions:
                board.bus_servo_set_position(
                    self.home_duration, self.home_positions
                )
                time.sleep(max(0.0, self.home_duration + 0.2))
            board.bus_servo_stop(self.servo_ids)
        except Exception as exc:  # noqa: BLE001
            raise DriverError(f"Hiwonder return_home failed: {exc}") from exc
        self._mode = DeviceMode.IDLE

    def emergency_stop(self) -> None:
        board = self._get_board()
        try:
            self._stop_and_release(board)
        except Exception as exc:  # noqa: BLE001
            raise DriverError(f"Hiwonder emergency_stop failed: {exc}") from exc
        self._mode = DeviceMode.E_STOP


register_arm_driver("simulated", SimulatedArmDriver)
register_arm_driver("http", HttpArmDriver)
register_arm_driver("hiwonder_bus_servo", HiwonderBusServoArmDriver)
