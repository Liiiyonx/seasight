"""WP-14 作业设备数字孪生 —— 协议层（冻结契约）。

本模块定义设备孪生与平台之间的**冻结接口**，独立于感知事件模拟器
（`edge/simulator/`），也独立于任何真实 MQTT 实现：

- 设备命令（平台 → 设备）：dispatch / pause / resume / return_home /
  emergency_stop / ack，命令信封固定携带
  `command_id, device_id, seq, issued_at, expires_at`。
- 遥测（设备 → 平台）：固定最小字段
  `device_id, seq, timestamp, mode, battery, position, velocity,
  bin_usage, mission_id, fault_code`。
- ACK（设备 → 平台）：平台侧用 :class:`AckTracker` 做幂等去重，
  重复 ACK / 乱序 ACK / 超时 ACK 都会被识别且只生效一次。
- 故障码固定集合：ack_timeout / duplicate_ack / out_of_order_ack /
  battery_critical / bin_full / gps_lost / communication_lost /
  emergency_stop，外加表示无故障的 "none"。

时间模型：内部统一使用**单调 epoch 秒**（float），便于假时钟做确定性
测试；`to_dict()` 序列化时同时给出 ISO-8601（UTC）字符串。

证据等级：本模块全部为 E1/E2 —— 孪生与测试可复现，不代表真实设备、
真实边缘盒或现场验证。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

# ----------------------------------------------------------------------
# 冻结常量
# ----------------------------------------------------------------------

#: 设备命令动作（冻结集合）。`ack` 是保留动作：孪生收到后做回执确认，
#: 不触发模式迁移（用于把冻结命令表补全，语义上等价平台侧 ping）。
COMMAND_ACTIONS: tuple[str, ...] = (
    "dispatch",
    "pause",
    "resume",
    "return_home",
    "emergency_stop",
    "ack",
)

#: 遥测最小字段（冻结集合，`to_dict()` 必须全部出现）。
TELEMETRY_FIELDS: tuple[str, ...] = (
    "device_id",
    "seq",
    "timestamp",
    "mode",
    "battery",
    "position",
    "velocity",
    "bin_usage",
    "mission_id",
    "fault_code",
)

#: 故障码（冻结集合，外加 "none"）。
FAULT_CODES: tuple[str, ...] = (
    "ack_timeout",
    "duplicate_ack",
    "out_of_order_ack",
    "battery_critical",
    "bin_full",
    "gps_lost",
    "communication_lost",
    "emergency_stop",
)

#: 多故障并存时，遥测 `fault_code` 只上报一个主故障，优先级从高到低。
#: 顺序即优先级：emergency_stop 最高。
FAULT_PRIORITY: tuple[str, ...] = (
    "emergency_stop",
    "communication_lost",
    "battery_critical",
    "bin_full",
    "gps_lost",
    "ack_timeout",
    "duplicate_ack",
    "out_of_order_ack",
)

FAULT_NONE = "none"


class DeviceMode:
    """设备运行模式（字符串常量，保持报文字面可读）。"""

    IDLE = "idle"
    NAVIGATING = "navigating"
    COLLECTING = "collecting"
    RETURNING = "returning"
    PAUSED = "paused"
    E_STOP = "emergency_stop"

    ALL: tuple[str, ...] = (IDLE, NAVIGATING, COLLECTING, RETURNING, PAUSED, E_STOP)


# ----------------------------------------------------------------------
# 时钟与 ID 工厂（可注入，测试用确定性实现）
# ----------------------------------------------------------------------
class Clock(Protocol):
    """可注入时钟：返回 epoch 秒。测试使用 :class:`FakeClock`。"""

    def now(self) -> float: ...


class FakeClock:
    """确定性假时钟：测试中手动推进，不依赖真实时间。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += max(0.0, float(seconds))

    @staticmethod
    def iso(t: float | None = None, timespec: str = "seconds") -> str:
        """把 epoch 秒格式化为 UTC ISO-8601（默认到秒，可字典序比较）。"""
        moment = datetime.fromtimestamp(t, tz=timezone.utc) if t is not None else datetime.now(timezone.utc)
        return moment.isoformat(timespec=timespec)


class IdFactory(Protocol):
    """可注入 ID 工厂。"""

    def next(self) -> str: ...


class SequentialIdFactory:
    """确定性 ID 工厂：`{prefix}_{n:04d}`，测试可复现。"""

    def __init__(self, prefix: str = "id") -> None:
        self.prefix = prefix
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return f"{self.prefix}_{self._n:04d}"


# ----------------------------------------------------------------------
# 领域对象
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Position:
    """WGS84 经纬度。"""

    lng: float
    lat: float

    def to_dict(self) -> dict[str, float]:
        return {"lng": round(float(self.lng), 6), "lat": round(float(self.lat), 6)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Position":
        return cls(lng=float(data["lng"]), lat=float(data["lat"]))


@dataclass
class DeviceCommand:
    """平台下发的设备命令（冻结信封）。

    五个信封字段全部必填：``command_id``（幂等键）、``device_id``、
    ``seq``（该设备的命令序号，用于判重与乱序识别）、``issued_at``、
    ``expires_at``（过期时间，超时未执行的命令判为失效）。
    """

    command_id: str
    device_id: str
    seq: int
    issued_at: float
    expires_at: float
    action: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "device_id": self.device_id,
            "seq": int(self.seq),
            "action": self.action,
            "issued_at": self.issued_at,
            "issued_at_iso": FakeClock.iso(self.issued_at),
            "expires_at": self.expires_at,
            "expires_at_iso": FakeClock.iso(self.expires_at),
            "params": self.params,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceCommand":
        """从报文 dict 解析；缺信封字段抛 ``KeyError``（由调用方捕获）。"""
        return cls(
            command_id=str(data["command_id"]),
            device_id=str(data["device_id"]),
            seq=int(data["seq"]),
            issued_at=float(data["issued_at"]),
            expires_at=float(data["expires_at"]),
            action=str(data["action"]),
            params=dict(data.get("params") or {}),
        )

    def is_expired(self, now: float) -> bool:
        return now > self.expires_at


@dataclass
class Ack:
    """设备对命令的回执。

    ``ack_id`` 为设备侧回执序号；``command_id`` 指向被确认的命令。
    平台按 ``command_id`` 幂等去重，所以重复投递同一 ACK 只生效一次。
    """

    ack_id: str
    command_id: str
    device_id: str
    seq: int
    received_at: float
    accepted: bool
    reason: str = ""
    mode: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ack_id": self.ack_id,
            "command_id": self.command_id,
            "device_id": self.device_id,
            "seq": int(self.seq),
            "received_at": self.received_at,
            "received_at_iso": FakeClock.iso(self.received_at),
            "accepted": bool(self.accepted),
            "reason": self.reason,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Ack":
        return cls(
            ack_id=str(data["ack_id"]),
            command_id=str(data["command_id"]),
            device_id=str(data["device_id"]),
            seq=int(data["seq"]),
            received_at=float(data["received_at"]),
            accepted=bool(data["accepted"]),
            reason=str(data.get("reason", "")),
            mode=str(data.get("mode", "")),
        )


@dataclass
class Telemetry:
    """设备遥测（冻结最小字段全部包含）。"""

    device_id: str
    seq: int
    timestamp: float
    mode: str
    battery: int
    position: Position | None
    velocity: float
    bin_usage: dict[str, float]
    mission_id: str | None
    fault_code: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "seq": int(self.seq),
            "timestamp": self.timestamp,
            "timestamp_iso": FakeClock.iso(self.timestamp),
            "mode": self.mode,
            "battery": int(self.battery),
            "position": self.position.to_dict() if self.position is not None else None,
            "velocity": round(float(self.velocity), 3),
            "bin_usage": {k: round(float(v), 4) for k, v in self.bin_usage.items()},
            "mission_id": self.mission_id,
            "fault_code": self.fault_code,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Telemetry":
        pos = data.get("position")
        return cls(
            device_id=str(data["device_id"]),
            seq=int(data["seq"]),
            timestamp=float(data["timestamp"]),
            mode=str(data["mode"]),
            battery=int(data["battery"]),
            position=Position.from_dict(pos) if isinstance(pos, dict) else None,
            velocity=float(data["velocity"]),
            bin_usage={k: float(v) for k, v in data["bin_usage"].items()},
            mission_id=data.get("mission_id"),
            fault_code=str(data["fault_code"]),
        )


# ----------------------------------------------------------------------
# ACK 幂等（平台侧接收端）
# ----------------------------------------------------------------------
class AckResult:
    """ACK 去重结果枚举（字符串常量）。"""

    NEW = "new"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    LATE = "late"

    ALL: tuple[str, ...] = (NEW, DUPLICATE, OUT_OF_ORDER, LATE)


class AckTracker:
    """平台侧 ACK 接收器：按 ``command_id`` 幂等去重。

    - **重复 ACK**：同一 ``command_id`` 第二次到达 → ``duplicate``，
      返回首次存储的规范回执，不重复生效。
    - **乱序 ACK**：按**到达顺序**判 —— 新回执的命令序号小于该设备
      已 ACK 的最大序号 → ``out_of_order``（仍记录一次，不丢结果）。
      注意判据是「已 ACK 的水位」，不是「已下发的命令水位」：
      多命令在途时，按序回执不应被误判为乱序。
    - **超时 ACK**：``received_at`` 晚于命令 ``expires_at`` → ``late``
      （回执仍然收下，平台据此判定「命令已过期但设备确实处理过」）。

    判定优先级：duplicate > late > out_of_order > new。
    """

    def __init__(self) -> None:
        self._acks: dict[str, Ack] = {}
        self._deadlines: dict[str, float] = {}
        self._max_acked_seq: dict[str, int] = {}
        self.outcomes: list[str] = []
        self.counts: dict[str, int] = {r: 0 for r in AckResult.ALL}

    def register_command(self, command: DeviceCommand) -> None:
        """命令下发时登记过期时间（供超时 ACK 判定）。"""
        self._deadlines[command.command_id] = command.expires_at

    def record(self, ack: Ack) -> str:
        """登记一条回执，返回 :class:`AckResult` 之一；幂等。"""
        existing = self._acks.get(ack.command_id)
        if existing is not None:
            result = AckResult.DUPLICATE
        else:
            self._acks[ack.command_id] = ack
            deadline = self._deadlines.get(ack.command_id)
            if deadline is not None and ack.received_at > deadline:
                result = AckResult.LATE
            elif ack.seq < self._max_acked_seq.get(ack.device_id, ack.seq):
                result = AckResult.OUT_OF_ORDER
            else:
                self._max_acked_seq[ack.device_id] = max(
                    self._max_acked_seq.get(ack.device_id, -1), ack.seq
                )
                result = AckResult.NEW
        self.outcomes.append(result)
        self.counts[result] += 1
        return result

    def is_acked(self, command_id: str) -> bool:
        return command_id in self._acks

    def ack_for(self, command_id: str) -> Ack | None:
        return self._acks.get(command_id)


# ----------------------------------------------------------------------
# 确定性摘要
# ----------------------------------------------------------------------
def payload_hash(payloads: list[dict[str, Any]]) -> str:
    """对一组报文做确定性 SHA-256 摘要（用于报告与重放比对）。"""
    body = json.dumps(
        payloads, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


__all__ = [
    "COMMAND_ACTIONS",
    "TELEMETRY_FIELDS",
    "FAULT_CODES",
    "FAULT_PRIORITY",
    "FAULT_NONE",
    "DeviceMode",
    "Clock",
    "FakeClock",
    "IdFactory",
    "SequentialIdFactory",
    "Position",
    "DeviceCommand",
    "Ack",
    "Telemetry",
    "AckResult",
    "AckTracker",
    "payload_hash",
]
