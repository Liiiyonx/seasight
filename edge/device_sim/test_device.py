"""WP-14 设备孪生测试 —— 命令、ACK、遥测、断网排队与急停优先级。

全部在**内存 transport + 假时钟 + 确定性 ID** 下运行：
- 不依赖真实 MQTT / broker / 网络；
- 时间、序号、ID 全部确定可复现；
- 覆盖冻结协议（命令信封五字段、遥测最小字段、八类故障码）。

证据等级：E1/E2 —— 只证明孪生协议与闭环在确定性环境中可复现，
不代表真实设备、真实边缘盒或现场验证。
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EDGE = HERE.parent
sys.path.insert(0, str(EDGE))

from device_sim.device import DeviceTwin, TwinConfig, ack_topic, telemetry_topic  # noqa: E402
from device_sim.protocol import (  # noqa: E402
    Ack,
    AckResult,
    AckTracker,
    DeviceCommand,
    DeviceMode,
    FakeClock,
    Position,
    SequentialIdFactory,
    TELEMETRY_FIELDS,
)
from device_sim.transport import MemoryTransport  # noqa: E402

DEVICE = "RBT-001"


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def make_cmd(
    device_id: str,
    command_id: str,
    seq: int,
    action: str,
    issued_at: float = 0.0,
    ttl: float = 30.0,
    **params: object,
) -> dict:
    """构造带冻结信封的命令报文。"""
    return {
        "command_id": command_id,
        "device_id": device_id,
        "seq": seq,
        "issued_at": issued_at,
        "expires_at": issued_at + ttl,
        "action": action,
        "params": params,
    }


def make_twin(
    device_id: str = DEVICE,
    config: TwinConfig | None = None,
    start: float = 0.0,
) -> tuple[DeviceTwin, MemoryTransport, FakeClock]:
    clock = FakeClock(start)
    transport = MemoryTransport()
    twin = DeviceTwin(
        device_id,
        transport,
        clock=clock,
        id_factory=SequentialIdFactory("ack"),
        config=config or TwinConfig(),
    )
    twin.start()  # 订阅 + 初始遥测
    return twin, transport, clock


def delivered_on(transport: MemoryTransport, topic: str) -> list[dict]:
    """读取真正被投递（路由成功）的报文。断网期间被丢弃的不算。"""
    return [payload for t, payload, q in transport.delivered if t == topic]


def acks_of(transport: MemoryTransport) -> list[Ack]:
    return [Ack.from_dict(p) for p in delivered_on(transport, ack_topic(DEVICE))]


def near_target(base: Position) -> dict:
    """基地附近的目标点（1~2 tick 内可达）。"""
    return {"lng": base.lng + 0.00005, "lat": base.lat}


# ----------------------------------------------------------------------
# 传输可注入性
# ----------------------------------------------------------------------
def test_transport_is_injectable_memory_transport() -> None:
    twin, transport, _ = make_twin()
    assert twin.transport is transport
    assert isinstance(transport, MemoryTransport)
    assert transport.connected
    transport.disconnect()
    assert not twin.transport.connected
    transport.connect()
    assert twin.transport.connected


def test_qos_conventions() -> None:
    twin, transport, _ = make_twin()
    twin.receive_command(make_cmd(DEVICE, "cmd_0001", 1, "pause"))
    twin.tick()
    seen_ack = seen_tel = False
    for topic, _payload, qos in transport.delivered:
        if topic == ack_topic(DEVICE):
            assert qos == 1
            seen_ack = True
        if topic == telemetry_topic(DEVICE):
            assert qos == 0
            seen_tel = True
    assert seen_ack and seen_tel


# ----------------------------------------------------------------------
# 命令信封（冻结五字段）
# ----------------------------------------------------------------------
def test_command_envelope_frozen_fields() -> None:
    raw = make_cmd(DEVICE, "cmd_0001", 1, "pause", issued_at=10.0, ttl=5.0)
    for key in ("command_id", "device_id", "seq", "issued_at", "expires_at"):
        assert key in raw
    cmd = DeviceCommand.from_dict(raw)
    assert cmd.command_id == "cmd_0001"
    assert cmd.device_id == DEVICE
    assert cmd.seq == 1
    assert cmd.issued_at == 10.0
    assert cmd.expires_at == 15.0
    assert not cmd.is_expired(14.0)
    assert cmd.is_expired(15.5)


def test_invalid_envelope_ignored() -> None:
    twin, transport, _ = make_twin()
    transport.deliver(twin.cmd_topic, {"command_id": "x"})  # 缺信封字段
    twin.tick()
    assert twin.counter["commands_invalid"] == 1
    assert twin.counter["commands_processed"] == 0


def test_unknown_action_rejected() -> None:
    twin, transport, _ = make_twin()
    twin.receive_command(make_cmd(DEVICE, "cmd_0001", 1, "fly"))
    twin.tick()
    acks = acks_of(transport)
    assert len(acks) == 1
    assert acks[0].accepted is False
    assert acks[0].reason == "unknown_action"


def test_dispatch_without_target_rejected() -> None:
    twin, transport, _ = make_twin()
    twin.receive_command(make_cmd(DEVICE, "cmd_0001", 1, "dispatch", task_id="tsk_001"))
    twin.tick()
    acks = acks_of(transport)
    assert acks[0].accepted is False
    assert acks[0].reason == "missing_target"
    assert twin.snapshot()["mode"] == DeviceMode.IDLE


def test_ack_reserved_action_is_echo() -> None:
    twin, transport, _ = make_twin()
    twin.receive_command(make_cmd(DEVICE, "cmd_0001", 1, "ack"))
    twin.tick()
    acks = acks_of(transport)
    assert acks[0].accepted is True
    assert acks[0].reason == "ack_echo"
    assert twin.snapshot()["mode"] == DeviceMode.IDLE


# ----------------------------------------------------------------------
# 命令 → 模式迁移闭环
# ----------------------------------------------------------------------
def test_dispatch_sets_mission_and_acks() -> None:
    twin, transport, _ = make_twin()
    twin.receive_command(
        make_cmd(DEVICE, "cmd_0001", 1, "dispatch", task_id="tsk_001",
                 target={"lng": 119.66, "lat": 26.39})
    )
    twin.tick()
    snap = twin.snapshot()
    assert snap["mode"] == DeviceMode.NAVIGATING
    assert snap["mission_id"] == "tsk_001"
    acks = acks_of(transport)
    assert len(acks) == 1
    assert acks[0].command_id == "cmd_0001"
    assert acks[0].accepted is True
    assert acks[0].reason == "dispatched"


def test_pause_resume_cycle() -> None:
    twin, transport, _ = make_twin()
    twin.receive_command(
        make_cmd(DEVICE, "cmd_0001", 1, "dispatch", task_id="tsk_001",
                 target={"lng": 119.66, "lat": 26.39})
    )
    twin.tick()
    assert twin.snapshot()["mode"] == DeviceMode.NAVIGATING
    twin.receive_command(make_cmd(DEVICE, "cmd_0002", 2, "pause"))
    twin.tick()
    assert twin.snapshot()["mode"] == DeviceMode.PAUSED
    twin.receive_command(make_cmd(DEVICE, "cmd_0003", 3, "resume"))
    twin.tick()
    assert twin.snapshot()["mode"] == DeviceMode.NAVIGATING


def test_out_of_order_command_arrival_reordered_by_seq() -> None:
    twin, transport, _ = make_twin()
    twin.receive_command(make_cmd(DEVICE, "cmd_0002", 2, "pause"))
    twin.receive_command(make_cmd(DEVICE, "cmd_0001", 1, "pause"))
    twin.tick()
    assert [a.seq for a in acks_of(transport)] == [1, 2]


def test_return_home_empties_bins_and_clears_mission() -> None:
    cfg = TwinConfig(bin_fill_per_tick=0.3)
    twin, transport, _ = make_twin(config=cfg)
    base = twin.config.base_position
    twin.receive_command(make_cmd(DEVICE, "cmd_0001", 1, "dispatch", task_id="tsk_001", target=near_target(base)))
    twin.tick()  # 到达目标 → collecting
    assert twin.snapshot()["mode"] == DeviceMode.COLLECTING
    twin.tick()  # 装仓
    assert sum(twin.snapshot()["bin_usage"].values()) > 0
    twin.receive_command(make_cmd(DEVICE, "cmd_0002", 2, "return_home"))
    twin.tick()  # → returning → 回基地 → 卸仓 → idle
    snap = twin.snapshot()
    assert snap["mode"] == DeviceMode.IDLE
    assert sum(snap["bin_usage"].values()) == 0.0
    assert snap["mission_id"] is None


# ----------------------------------------------------------------------
# ACK 幂等（重复 / 乱序 / 超时）
# ----------------------------------------------------------------------
def test_ack_tracker_duplicate() -> None:
    tracker = AckTracker()
    cmd = DeviceCommand("c1", "RBT", 1, 0.0, 10.0, "pause")
    tracker.register_command(cmd)
    assert tracker.record(Ack("a1", "c1", "RBT", 1, 5.0, True)) == AckResult.NEW
    # 同 command_id 再次到达 → duplicate，规范回执仍是首次
    assert tracker.record(Ack("a2", "c1", "RBT", 1, 6.0, True)) == AckResult.DUPLICATE
    assert tracker.ack_for("c1").ack_id == "a1"


def test_ack_tracker_out_of_order() -> None:
    tracker = AckTracker()
    tracker.register_command(DeviceCommand("c1", "RBT", 1, 0.0, 10.0, "pause"))
    tracker.register_command(DeviceCommand("c2", "RBT", 2, 0.0, 10.0, "pause"))
    assert tracker.record(Ack("a2", "c2", "RBT", 2, 5.0, True)) == AckResult.NEW
    # 序号 1 的回执晚于序号 2 → 乱序，但结果仍记录一次
    assert tracker.record(Ack("a1", "c1", "RBT", 1, 6.0, True)) == AckResult.OUT_OF_ORDER
    assert tracker.counts[AckResult.OUT_OF_ORDER] == 1


def test_ack_tracker_late() -> None:
    tracker = AckTracker()
    tracker.register_command(DeviceCommand("c1", "RBT", 1, 0.0, 5.0, "pause"))  # 5s 过期
    late = Ack("a1", "c1", "RBT", 1, 6.0, True)
    assert tracker.record(late) == AckResult.LATE
    # 超时后再重复 → 仍幂等
    assert tracker.record(late) == AckResult.DUPLICATE


def test_duplicate_command_ack_is_idempotent() -> None:
    twin, transport, _ = make_twin()
    tracker = AckTracker()
    cmd = DeviceCommand.from_dict(make_cmd(DEVICE, "cmd_0001", 1, "pause"))
    tracker.register_command(cmd)
    twin.receive_command(cmd)
    twin.tick()
    twin.receive_command(cmd)  # 同 command_id 重复到达 → 幂等复回，不重复执行
    twin.tick()
    acks = acks_of(transport)
    assert len(acks) == 2
    assert [tracker.record(a) for a in acks] == [AckResult.NEW, AckResult.DUPLICATE]


def test_duplicate_ack_fault_publishes_twice() -> None:
    twin, transport, _ = make_twin()
    twin.inject_fault("duplicate_ack")
    cmd = DeviceCommand.from_dict(make_cmd(DEVICE, "cmd_0001", 1, "pause"))
    twin.receive_command(cmd)
    twin.tick()
    acks = acks_of(transport)
    assert len(acks) == 2
    assert acks[0].ack_id == acks[1].ack_id  # 同一回执重复投递
    tracker = AckTracker()
    tracker.register_command(cmd)
    assert [tracker.record(a) for a in acks] == [AckResult.NEW, AckResult.DUPLICATE]


def test_out_of_order_ack_fault_reverses_ack_order() -> None:
    twin, transport, _ = make_twin()
    twin.inject_fault("out_of_order_ack")
    twin.receive_command(make_cmd(DEVICE, "cmd_0001", 1, "pause"))
    twin.receive_command(make_cmd(DEVICE, "cmd_0002", 2, "pause"))
    twin.tick()
    assert [a.seq for a in acks_of(transport)] == [2, 1]


def test_ack_timeout_fault_suppresses_acks() -> None:
    twin, transport, _ = make_twin()
    twin.inject_fault("ack_timeout")
    twin.receive_command(make_cmd(DEVICE, "cmd_0001", 1, "pause"))
    twin.tick()
    assert acks_of(transport) == []
    assert twin.counter["acks_suppressed"] == 1
    assert twin.counter["acks_published"] == 0


def test_expired_command_rejected_with_late_ack() -> None:
    twin, transport, clock = make_twin(start=0.0)
    raw = make_cmd(DEVICE, "cmd_0001", 1, "pause", issued_at=0.0, ttl=5.0)
    twin.receive_command(raw)
    clock.advance(10.0)  # 处理时已超过 expires_at
    twin.tick()
    acks = acks_of(transport)
    assert len(acks) == 1
    assert acks[0].accepted is False
    assert acks[0].reason == "expired"
    assert twin.counter["commands_rejected"] == 1
    tracker = AckTracker()
    tracker.register_command(DeviceCommand.from_dict(raw))
    assert tracker.record(acks[0]) == AckResult.LATE


# ----------------------------------------------------------------------
# 遥测最小字段与电量 / 仓容 / 位置时间戳
# ----------------------------------------------------------------------
def test_telemetry_always_contains_frozen_fields() -> None:
    twin, transport, clock = make_twin()
    base = twin.config.base_position
    twin.receive_command(
        make_cmd(DEVICE, "cmd_0001", 1, "dispatch", task_id="tsk_001",
                 target={"lng": base.lng + 0.0005, "lat": base.lat + 0.0003})
    )
    for _ in range(6):
        clock.advance(1.0)
        twin.tick()
    telems = delivered_on(transport, telemetry_topic(DEVICE))
    assert len(telems) >= 7
    for tm in telems:
        for key in TELEMETRY_FIELDS:
            assert key in tm, key


def test_telemetry_battery_bin_position_timestamp_values() -> None:
    cfg = TwinConfig(bin_fill_per_tick=0.1)  # 4 tick 内总量 <0.95，不会触发自动返航
    twin, transport, clock = make_twin(config=cfg)
    base = twin.config.base_position
    twin.receive_command(
        make_cmd(DEVICE, "cmd_0001", 1, "dispatch", task_id="tsk_001",
                 target={"lng": base.lng + 0.00005, "lat": base.lat})
    )
    for _ in range(4):
        clock.advance(1.0)
        twin.tick()
    telems = delivered_on(transport, telemetry_topic(DEVICE))
    # 电量：作业中单调下降
    batteries = [tm["battery"] for tm in telems]
    assert batteries[-1] < batteries[0]
    # 时间戳：单调不减
    stamps = [tm["timestamp"] for tm in telems]
    assert stamps == sorted(stamps)
    # 位置：从基地向目标移动，字段为 {"lng","lat"}
    first_pos = telems[0]["position"]
    assert first_pos == {"lng": round(base.lng, 6), "lat": round(base.lat, 6)}
    moved = [tm for tm in telems if tm["position"] != first_pos]
    assert moved, "位置应随航行移动"
    for tm in telems:
        assert {"lng", "lat"} <= set(tm["position"])
    # 仓容：collecting 后上升
    assert telems[-1]["bin_usage"]["foam"] > 0.0
    # 模式出现在遥测中
    assert telems[-1]["mode"] in DeviceMode.ALL


def test_telemetry_velocity_active_vs_idle() -> None:
    twin, transport, _ = make_twin()
    telems = delivered_on(transport, telemetry_topic(DEVICE))
    assert telems[0]["velocity"] == 0.0  # idle 初始
    twin.receive_command(
        make_cmd(DEVICE, "cmd_0001", 1, "dispatch", task_id="tsk_001",
                 target={"lng": 119.66, "lat": 26.39})
    )
    twin.tick()
    telems = delivered_on(transport, telemetry_topic(DEVICE))
    assert telems[-1]["mode"] == DeviceMode.NAVIGATING
    assert telems[-1]["velocity"] > 0.0


def test_gps_lost_position_none_and_recovery() -> None:
    twin, transport, clock = make_twin()
    twin.inject_fault("gps_lost")
    clock.advance(1.0)
    twin.tick()
    telems = delivered_on(transport, telemetry_topic(DEVICE))
    assert telems[-1]["fault_code"] == "gps_lost"
    assert telems[-1]["position"] is None
    twin.clear_fault("gps_lost")
    clock.advance(1.0)
    twin.tick()
    telems = delivered_on(transport, telemetry_topic(DEVICE))
    assert telems[-1]["fault_code"] == "none"
    assert telems[-1]["position"] is not None


# ----------------------------------------------------------------------
# 断网排队 / 重连补传
# ----------------------------------------------------------------------
def test_offline_queueing_and_reconnect_resend_in_seq_order() -> None:
    twin, transport, clock = make_twin()
    transport.disconnect()
    twin.tick()
    twin.tick()
    assert twin.counter["buffered"] == 2
    assert twin.counter["flushed"] == 0
    clock.advance(1.0)
    transport.connect()
    twin.tick()  # 下一 tick 检测到重连 → 按序补传
    assert twin.counter["flushed"] == 2
    telems = delivered_on(transport, telemetry_topic(DEVICE))
    seqs = [tm["seq"] for tm in telems]
    assert seqs == [1, 2, 3, 4]  # 初始 1 + 补传 2,3 + 新 4，seq 严格递增
    assert seqs == sorted(seqs)


def test_communication_lost_fault_queues_and_reconnect_flushes() -> None:
    twin, transport, clock = make_twin()
    twin.inject_fault("communication_lost")
    assert not transport.connected
    twin.tick()
    twin.tick()
    assert twin.counter["buffered"] == 2
    assert twin.counter["flushed"] == 0
    twin.clear_fault("communication_lost")
    assert transport.connected
    clock.advance(1.0)
    twin.tick()
    assert twin.counter["flushed"] == 2
    telems = delivered_on(transport, telemetry_topic(DEVICE))
    assert [tm["seq"] for tm in telems] == [1, 2, 3, 4]


# ----------------------------------------------------------------------
# 紧急停止优先级
# ----------------------------------------------------------------------
def test_emergency_stop_priority_over_queued_commands() -> None:
    twin, transport, _ = make_twin()
    twin.receive_command(
        make_cmd(DEVICE, "cmd_0001", 1, "dispatch", task_id="tsk_001",
                 target={"lng": 119.66, "lat": 26.39})
    )
    twin.receive_command(make_cmd(DEVICE, "cmd_0002", 2, "pause"))
    twin.receive_command(make_cmd(DEVICE, "cmd_0003", 3, "emergency_stop"))
    twin.tick()
    assert twin.snapshot()["mode"] == DeviceMode.E_STOP
    acks = acks_of(transport)
    assert acks[0].command_id == "cmd_0003"  # 急停最先执行并优先回执
    assert acks[0].accepted is True
    by_id = {a.command_id: a for a in acks}
    assert by_id["cmd_0001"].accepted is False
    assert by_id["cmd_0001"].reason == "device_in_emergency_stop"
    assert by_id["cmd_0002"].accepted is False
    assert by_id["cmd_0002"].reason == "device_in_emergency_stop"
    # resume 恢复急停 → 回到急停前模式
    twin.receive_command(make_cmd(DEVICE, "cmd_0004", 4, "resume"))
    twin.tick()
    assert twin.snapshot()["mode"] == DeviceMode.IDLE
    assert acks_of(transport)[-1].reason == "recovered_from_emergency_stop"


def test_injected_emergency_stop_forces_mode_and_clear_recovers() -> None:
    twin, transport, clock = make_twin()
    twin.inject_fault("emergency_stop")
    assert twin.snapshot()["mode"] == DeviceMode.E_STOP
    clock.advance(1.0)
    twin.tick()
    telems = delivered_on(transport, telemetry_topic(DEVICE))
    assert telems[-1]["fault_code"] == "emergency_stop"
    twin.clear_fault("emergency_stop")
    assert twin.snapshot()["mode"] == DeviceMode.IDLE
    assert twin.counter["faults_recovered"] == 1


# ----------------------------------------------------------------------
# 天然故障与恢复闭环（低电量 / 仓满）
# ----------------------------------------------------------------------
def test_battery_critical_natural_fault_auto_return_and_recovery() -> None:
    cfg = TwinConfig(
        initial_battery=20,
        battery_critical_threshold=15,
        battery_drain_per_tick=2.0,
        battery_recharge_per_tick=2.0,
        bin_fill_per_tick=0.0,
    )
    twin, transport, clock = make_twin(config=cfg)
    base = twin.config.base_position
    twin.receive_command(
        make_cmd(DEVICE, "cmd_0001", 1, "dispatch", task_id="tsk_001",
                 target={"lng": base.lng + 0.001, "lat": base.lat})
    )
    twin.tick()
    assert twin.snapshot()["mode"] == DeviceMode.NAVIGATING
    twin.tick()  # 电量 16
    twin.tick()  # 电量 14 < 15 → 低电自动返航
    assert twin.snapshot()["mode"] == DeviceMode.RETURNING
    telems = delivered_on(transport, telemetry_topic(DEVICE))
    assert telems[-1]["fault_code"] == "battery_critical"
    # 回到基地卸下任务 → 充电 → 故障码恢复为 none
    for _ in range(30):
        clock.advance(1.0)
        twin.tick()
        snap = twin.snapshot()
        if snap["mode"] == DeviceMode.IDLE and snap["fault_code"] == "none":
            break
    assert twin.snapshot()["mode"] == DeviceMode.IDLE
    assert twin.snapshot()["fault_code"] == "none"


def test_bin_full_auto_return_and_dump() -> None:
    cfg = TwinConfig(bin_fill_per_tick=0.4, bin_full_threshold=0.9, battery_drain_per_tick=0.0)
    twin, transport, clock = make_twin(config=cfg)
    base = twin.config.base_position
    twin.receive_command(
        make_cmd(DEVICE, "cmd_0001", 1, "dispatch", task_id="tsk_001", target=near_target(base))
    )
    twin.tick()  # 到达 → collecting
    assert twin.snapshot()["mode"] == DeviceMode.COLLECTING
    twin.tick()  # 装仓总量 ≥ 0.9 → 自动返航
    assert twin.snapshot()["mode"] == DeviceMode.RETURNING
    twin.tick()  # 回基地 → 卸仓 → idle，故障恢复
    snap = twin.snapshot()
    assert snap["mode"] == DeviceMode.IDLE
    assert sum(snap["bin_usage"].values()) == 0.0
    assert snap["fault_code"] == "none"
