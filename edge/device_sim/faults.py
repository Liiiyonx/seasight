"""WP-14 故障注入 —— 脚本化场景与确定性报告。

在假时钟 + 内存 transport + 确定性 ID 下，把「命令 → ACK → 遥测 →
故障 → 恢复」驱动成一个可复现的脚本场景，并输出确定性报告：

- :class:`DeviceScenario`：按时间轴声明命令、故障注入/恢复、断网/恢复。
- :func:`run_device_scenario`：执行场景，返回 :class:`FaultReport`。
- :class:`FaultReport`：结构化报告（JSON 可序列化、可哈希比对），
  含 ACK 幂等统计（重复/乱序/超时）、遥测故障观测、注入故障的
  检测与恢复、以及 ``device_fault_recovery_rate``。

确定性保证：固定 ``seed`` + 相同场景 → 逐字节相同的 JSON 报告。
``noise=True`` 时用 ``seed`` 播种的 ``random.Random`` 给遥测加抖动，
并把抖动载荷的 SHA-256 放进报告（``noise_hash``），用于证明
「种子确实影响输出」且同种子可复现。

全部为 E1/E2：只证明孪生协议与故障闭环在确定性环境中可复现，
不代表真实设备、真实边缘盒或现场验证。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any

from .device import DeviceTwin, TwinConfig
from .protocol import (
    AckResult,
    AckTracker,
    DeviceCommand,
    FakeClock,
    Position,
    SequentialIdFactory,
    payload_hash,
)
from .transport import MemoryTransport

#: 命令默认有效期（秒）。场景命令未显式传 ttl 时使用。
DEFAULT_COMMAND_TTL = 30.0


# ----------------------------------------------------------------------
# 场景定义
# ----------------------------------------------------------------------
@dataclass
class ScenarioEvent:
    """时间轴上的一个场景事件。

    ``kind`` 取值：
    - "command"   —— 下发命令（``action`` + ``params``）
    - "fault"     —— 注入故障（``fault``）
    - "recover"   —— 清除故障（``fault``）
    - "disconnect"—— 模拟断网（transport 离线）
    - "connect"   —— 模拟恢复连接
    """

    at: float
    kind: str
    action: str | None = None
    fault: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


class DeviceScenario:
    """脚本化设备孪生场景（链式构建）。"""

    def __init__(self, scenario_id: str = "device_scenario") -> None:
        self.scenario_id = scenario_id
        self.events: list[ScenarioEvent] = []

    def cmd(self, at: float, action: str, **params: Any) -> "DeviceScenario":
        """在 ``at`` 时刻下发命令。"""
        self.events.append(ScenarioEvent(at=at, kind="command", action=action, params=params))
        return self

    def inject(self, at: float, fault: str) -> "DeviceScenario":
        self.events.append(ScenarioEvent(at=at, kind="fault", fault=fault))
        return self

    def recover(self, at: float, fault: str) -> "DeviceScenario":
        self.events.append(ScenarioEvent(at=at, kind="recover", fault=fault))
        return self

    def disconnect(self, at: float) -> "DeviceScenario":
        self.events.append(ScenarioEvent(at=at, kind="disconnect"))
        return self

    def connect(self, at: float) -> "DeviceScenario":
        self.events.append(ScenarioEvent(at=at, kind="connect"))
        return self

    def sorted_events(self) -> list[ScenarioEvent]:
        return sorted(self.events, key=lambda e: (e.at, e.kind))

    def __len__(self) -> int:
        return len(self.events)


# ----------------------------------------------------------------------
# 确定性报告
# ----------------------------------------------------------------------
class FaultReport:
    """故障场景的确定性报告（可 JSON 序列化、可逐字节比对）。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        return self.data

    def to_json(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FaultReport):
            return NotImplemented
        return self.to_json() == other.to_json()

    def __hash__(self) -> int:  # 只为可比较（不可变快照由 to_json 决定）
        return hash(self.to_json())

    @property
    def recovery_rate(self) -> float | None:
        return self.data.get("device_fault_recovery_rate")

    @property
    def ack_outcomes(self) -> dict[str, int]:
        return self.data["ack_outcomes"]

    @property
    def faults_injected(self) -> list[dict[str, Any]]:
        return self.data["faults_injected"]


# ----------------------------------------------------------------------
# 场景执行器
# ----------------------------------------------------------------------
def run_device_scenario(
    device_id: str,
    scenario: DeviceScenario,
    *,
    seed: int = 42,
    start: float = 0.0,
    end_at: float | None = None,
    config: TwinConfig | None = None,
    noise: bool = False,
) -> FaultReport:
    """执行场景，返回确定性 :class:`FaultReport`。

    - 时钟从 ``start`` 起步；在每个场景事件时刻与 ``end_at`` 各跑一个
      tick（未显式给 ``end_at`` 时取最后事件时刻 + 1s）。
    - 命令信封由平台侧确定性生成：``command_id=cmd_{n:04d}``、
      ``seq=1..N``、``issued_at=at``、``expires_at=at+ttl``。
    - 平台侧 :class:`AckTracker` 登记每条下发命令并统计回执幂等结果。
    - ``noise=True`` 时给遥测加种子化抖动，报告附带 ``noise_hash``。
    """
    events = scenario.sorted_events()
    last_event_at = max((e.at for e in events), default=start)
    end_at = float(end_at) if end_at is not None else last_event_at + 1.0
    end_at = max(end_at, last_event_at + 0.0)

    clock = FakeClock(start)
    transport = MemoryTransport()
    event_sink: list[dict[str, Any]] = []
    rng = random.Random(seed)
    modifier = (lambda tm: _jitter_telemetry(tm, rng)) if noise else None
    twin = DeviceTwin(
        device_id,
        transport,
        clock=clock,
        id_factory=SequentialIdFactory("ack"),
        config=config or TwinConfig(),
        event_sink=event_sink,
        telemetry_modifier=modifier,
    )

    tracker = AckTracker()
    telemetry_received: list[dict[str, Any]] = []
    acks_received: list[dict[str, Any]] = []

    def on_telemetry(topic: str, payload: dict[str, Any]) -> None:
        telemetry_received.append(payload)

    def on_ack(topic: str, payload: dict[str, Any]) -> None:
        acks_received.append(payload)
        tracker.record(_ack_from_payload(payload))

    transport.subscribe(twin.telemetry_topic, on_telemetry)
    transport.subscribe(twin.ack_topic, on_ack)

    twin.start()  # 订阅 + 初始遥测

    # 平台侧命令序号
    plat_seq = 0

    def issue_command(ev: ScenarioEvent) -> None:
        nonlocal plat_seq
        plat_seq += 1
        params = dict(ev.params)  # 复制，避免污染场景对象（可重复运行）
        ttl = float(params.pop("ttl", DEFAULT_COMMAND_TTL))
        cmd = DeviceCommand(
            command_id=f"cmd_{plat_seq:04d}",
            device_id=device_id,
            seq=plat_seq,
            issued_at=ev.at,
            expires_at=ev.at + ttl,
            action=ev.action or "",
            params=params,
        )
        tracker.register_command(cmd)
        event_sink.append(
            {
                "t": round(ev.at, 6),
                "type": "command_issued",
                "command_id": cmd.command_id,
                "action": cmd.action,
                "seq": cmd.seq,
                "expires_at": cmd.expires_at,
            }
        )
        transport.deliver(twin.cmd_topic, cmd.to_dict(), qos=1)

    def apply_event(ev: ScenarioEvent) -> None:
        if ev.kind == "command":
            issue_command(ev)
        elif ev.kind == "fault":
            twin.inject_fault(ev.fault or "")
        elif ev.kind == "recover":
            twin.clear_fault(ev.fault or "")
        elif ev.kind == "disconnect":
            transport.disconnect()
            event_sink.append({"t": round(ev.at, 6), "type": "transport_offline"})
        elif ev.kind == "connect":
            transport.connect()
            event_sink.append({"t": round(ev.at, 6), "type": "transport_online"})

    # ---- 执行：在事件时刻 tick（twin.start() 已在 start 时刻发过初始遥测）----
    idx = 0
    event_times = {e.at for e in events if e.at >= start}
    times = sorted(event_times | {end_at})
    cursor = start
    steps = 0
    for t in times:
        if t > cursor:
            clock.advance(t - cursor)
            cursor = t
        while idx < len(events) and events[idx].at <= cursor:
            apply_event(events[idx])
            idx += 1
        telemetry = twin.tick()
        steps += 1

    twin.stop()

    # ---- 遥测故障观测 ----
    fault_observations: dict[str, dict[str, Any]] = {}
    detected_total = 0
    for tm in telemetry_received:
        code = tm["fault_code"]
        if code != "none":
            detected_total += 1
        obs = fault_observations.setdefault(code, {"samples": 0, "first_seen": None, "last_seen": None})
        obs["samples"] += 1
        if obs["first_seen"] is None:
            obs["first_seen"] = tm["timestamp"]
        obs["last_seen"] = tm["timestamp"]

    # ---- 注入故障生命周期 ----
    faults_injected: list[dict[str, Any]] = []
    recovered = 0
    for code, lc in twin._fault_lifecycle.items():
        injected_at = lc.get("injected_at")
        cleared_at = lc.get("cleared_at")
        is_recovered = cleared_at is not None
        if is_recovered:
            recovered += 1
        faults_injected.append(
            {
                "fault": code,
                "injected_at": injected_at,
                "cleared_at": cleared_at,
                "recovered": is_recovered,
            }
        )
    total_injected = len(faults_injected)
    recovery_rate = (
        round(recovered / total_injected, 4) if total_injected > 0 else None
    )

    # ---- 噪声哈希（noise=True 时）----
    noise_hash = None
    if noise:
        noise_hash = payload_hash(
            [
                {
                    "seq": tm["seq"],
                    "position": tm["position"],
                    "battery": tm["battery"],
                    "velocity": tm["velocity"],
                }
                for tm in telemetry_received
            ]
        )

    data: dict[str, Any] = {
        "scenario_id": scenario.scenario_id,
        "device_id": device_id,
        "seed": seed,
        "started_at": start,
        "finished_at": end_at,
        "steps": steps,
        "noise": noise,
        "commands_issued": plat_seq,
        "commands_processed": twin.counter["commands_processed"],
        "commands_rejected": twin.counter["commands_rejected"],
        "commands_invalid": twin.counter["commands_invalid"],
        "acks_published": twin.counter["acks_published"],
        "acks_suppressed": twin.counter["acks_suppressed"],
        "ack_outcomes": dict(tracker.counts),
        "telemetry_count": len(telemetry_received),
        "telemetry_buffered": twin.counter["buffered"],
        "telemetry_flushed": twin.counter["flushed"],
        "fault_detected_telemetry": detected_total,
        "fault_observations": fault_observations,
        "faults_injected": faults_injected,
        "faults_recovered": recovered,
        "device_fault_recovery_rate": recovery_rate,
        "noise_hash": noise_hash,
        "events": event_sink,
    }
    return FaultReport(data)


def _ack_from_payload(payload: dict[str, Any]) -> Any:
    """把 ACK 报文 dict 转回 Ack 对象（复用 Ack.from_dict）。"""
    from .protocol import Ack

    return Ack.from_dict(payload)


def _jitter_telemetry(telemetry: Any, rng: random.Random) -> None:
    """给刚构造的遥测加种子化抖动（仅 noise=True 时，用于确定性证明）。"""
    if telemetry.position is not None:
        telemetry.position = Position(
            telemetry.position.lng + rng.uniform(-1e-6, 1e-6),
            telemetry.position.lat + rng.uniform(-1e-6, 1e-6),
        )
    telemetry.battery = max(0, min(100, telemetry.battery + rng.choice([-1, 0, 1])))
    telemetry.velocity = round(telemetry.velocity + rng.uniform(-0.05, 0.05), 3)


__all__ = [
    "DEFAULT_COMMAND_TTL",
    "ScenarioEvent",
    "DeviceScenario",
    "FaultReport",
    "run_device_scenario",
]
