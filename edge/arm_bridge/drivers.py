"""Arm driver adapters.

``ArmDriver`` is the only interface a new arm has to implement. The bridge and
the platform MQTT contract stay unchanged when hardware changes.

Evidence level: E1/E2. Simulated and generic HTTP drivers are deterministic
prototype adapters; they do not prove physical pickup or field acceptance.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from device_sim.protocol import DeviceMode, Position


class DriverError(RuntimeError):
    """Raised when the underlying arm SDK rejects or fails an action."""


@dataclass(frozen=True)
class PickResult:
    """Outcome of one pickup action, translated to the platform contract."""

    ok: bool = True
    collected_weight: float = 0.0
    review_result: str = "confirmed"
    evidence_url: str | None = None
    bins_after: dict[str, float] = field(
        default_factory=lambda: {"foam": 0.0, "plastic": 0.0, "mixed": 0.0}
    )


@dataclass(frozen=True)
class ArmStatus:
    """Current status snapshot reported as marine telemetry."""

    mode: str = DeviceMode.IDLE
    battery: int = 100
    bins: dict[str, float] = field(
        default_factory=lambda: {"foam": 0.0, "plastic": 0.0, "mixed": 0.0}
    )
    location: Position = Position(119.6540, 26.3870)
    heading: float = 0.0
    speed: float = 0.0


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


ArmDriverFactory = Callable[..., Any]

ARM_DRIVER_REGISTRY: dict[str, ArmDriverFactory] = {}


def register_arm_driver(name: str, factory: ArmDriverFactory) -> None:
    """Register a named arm driver so config can select it without code changes."""
    driver_name = str(name).strip()
    if not driver_name:
        raise ValueError("driver name must not be empty")
    ARM_DRIVER_REGISTRY[driver_name] = factory


def build_arm_driver(
    driver_cfg: dict[str, Any],
    backend: str | None = None,
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
        bins: dict[str, float] | None = None,
        home: dict[str, float] | None = None,
        collected_weight: float = 0.05,
        review_result: str = "confirmed",
    ) -> None:
        self.battery = int(battery)
        self.bins: dict[str, float] = dict(
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
        headers: dict[str, str] | None = None,
        battery: int = 100,
        bins: dict[str, float] | None = None,
        home: dict[str, float] | None = None,
    ) -> None:
        self.endpoint = str(endpoint).rstrip("/")
        self.method = str(method).upper()
        self.timeout = float(timeout)
        self.headers = {"Content-Type": "application/json"}
        self.headers.update(headers or {})
        self.battery = int(battery)
        self.bins: dict[str, float] = dict(
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

    def _call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
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
        servo_ids: list[int] | None = None,
        home_positions: list[list[int]] | None = None,
        pick_sequence: list[dict[str, Any]] | None = None,
        pick_sequence_file: str | None = None,
        pick_positions: list[list[int]] | None = None,
        pick_duration: float = 1.0,
        home_duration: float = 1.5,
        collected_weight: float = 0.0,
        review_result: str = "recheck",
        evidence_url: str | None = None,
        battery: int = 100,
        bins: dict[str, float] | None = None,
        home: dict[str, float] | None = None,
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
        self.bins: dict[str, float] = dict(
            bins or {"foam": 0.0, "plastic": 0.0, "mixed": 0.0}
        )
        home = home or {"lng": 119.6540, "lat": 26.3870}
        self.home = Position(float(home["lng"]), float(home["lat"]))
        self._mode = DeviceMode.IDLE
        self._board: Any | None = None

    @staticmethod
    def _normalize_positions(positions: Any) -> list[list[int]]:
        normalized: list[list[int]] = []
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
    def _normalize_sequence(cls, sequence: Any) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
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
    def _load_sequence_file(cls, path: str) -> list[dict[str, Any]]:
        """Load a pick sequence JSON file written by the teach tool."""
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
        return cls._normalize_sequence(raw)

    def _new_board(self) -> Any:
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

    def status(self) -> ArmStatus:
        battery = self.battery
        if self._board is not None:
            try:
                self._board.enable_reception(True)
                raw = self._board.get_battery()
                if isinstance(raw, (int, float)) and raw > 0:
                    # Vendor reports raw mV; keep the configured prototype
                    # percentage until a calibrated mapping exists.
                    battery = self.battery
            except Exception:  # noqa: BLE001
                pass
        return ArmStatus(
            mode=self._mode,
            battery=battery,
            bins=dict(self.bins),
            location=self.home,
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
