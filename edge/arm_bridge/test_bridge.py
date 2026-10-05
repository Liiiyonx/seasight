"""Arm bridge tests using the in-memory transport (E1/E2).

No broker, no hardware, no network. The tests prove the MQTT contract loop:
task -> ACK -> collecting -> driver pick -> done -> marine telemetry.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EDGE = HERE.parent
if str(EDGE) not in sys.path:
    sys.path.insert(0, str(EDGE))

from arm_bridge.bridge import ArmBridge  # noqa: E402
from arm_bridge.drivers import (  # noqa: E402
    ARM_DRIVER_REGISTRY,
    DriverError,
    HiwonderBusServoArmDriver,
    HttpArmDriver,
    SimulatedArmDriver,
    build_arm_driver,
)
from device_sim.protocol import (  # noqa: E402
    DeviceCommand,
    DeviceMode,
    FakeClock,
    Position,
    SequentialIdFactory,
)
from device_sim.transport import MemoryTransport  # noqa: E402

DEVICE = "RBT-ARM-01"
SITE = "lianjiang"


def make_command(
    command_id: str,
    action: str,
    seq: int = 1,
    *,
    issued_at: float = 0.0,
    ttl: float = 30.0,
    **params: object,
) -> dict:
    return {
        "command_id": command_id,
        "device_id": DEVICE,
        "seq": seq,
        "action": action,
        "issued_at": issued_at,
        "expires_at": issued_at + ttl,
        "params": params,
    }


def dispatch_payload(
    task_id: str = "tsk_0001", issued_at: float = 0.0, ttl: float = 30.0
) -> dict:
    return make_command(
        f"cmd_{task_id}",
        "dispatch",
        task_id=task_id,
        target={"lng": 119.6531, "lat": 26.3867},
        priority=1,
        issued_at=issued_at,
        ttl=ttl,
    )


def make_bridge(start: float = 0.0) -> tuple[ArmBridge, MemoryTransport, FakeClock]:
    transport = MemoryTransport()
    clock = FakeClock(start)
    driver = SimulatedArmDriver()
    bridge = ArmBridge(
        DEVICE,
        SITE,
        driver,
        transport,
        clock=clock,
        id_factory=SequentialIdFactory("ack"),
    )
    bridge.start()
    return bridge, transport, clock


def published_on(transport: MemoryTransport, topic: str) -> list[dict]:
    return [payload for t, payload, qos in transport.published if t == topic]


def test_full_dispatch_loop() -> None:
    bridge, transport, clock = make_bridge()
    payload = dispatch_payload("tsk_0001")

    transport.deliver(bridge.task_topic, payload, qos=1)

    acks = published_on(transport, bridge.ack_topic)
    assert len(acks) == 1
    assert acks[0]["command_id"] == "cmd_tsk_0001"
    assert acks[0]["accepted"] is True
    assert acks[0]["mode"] == DeviceMode.NAVIGATING

    progress = published_on(transport, bridge.progress_topic)
    assert [p["status"] for p in progress] == ["collecting", "done"]
    assert progress[-1]["task_id"] == "tsk_0001"
    assert progress[-1]["robot_id"] == DEVICE
    assert progress[-1]["review_result"] == "confirmed"
    assert progress[-1]["collected_weight"] == 0.05

    telemetry = published_on(transport, bridge.telemetry_topic)
    assert len(telemetry) >= 2
    assert telemetry[0]["status"] == DeviceMode.COLLECTING
    assert telemetry[-1]["status"] == DeviceMode.IDLE
    assert telemetry[-1]["device_type"] == "robot"
    assert telemetry[-1]["task_id"] == "tsk_0001"

    assert bridge.counter["pick_executed"] == 1


def test_duplicate_command_is_idempotent() -> None:
    bridge, transport, _ = make_bridge()
    payload = dispatch_payload("tsk_0002")

    transport.deliver(bridge.task_topic, payload, qos=1)
    transport.deliver(bridge.task_topic, payload, qos=1)

    assert bridge.counter["duplicates"] == 1
    assert bridge.counter["pick_executed"] == 1
    assert len(published_on(transport, bridge.progress_topic)) == 2  # one loop only
    assert len(published_on(transport, bridge.ack_topic)) == 2  # original + replay


def test_expired_command_rejected() -> None:
    bridge, transport, clock = make_bridge()
    clock.advance(10.0)
    payload = dispatch_payload("tsk_0003", issued_at=0.0, ttl=5.0)

    transport.deliver(bridge.task_topic, payload, qos=1)

    acks = published_on(transport, bridge.ack_topic)
    assert len(acks) == 1
    assert acks[0]["accepted"] is False
    assert acks[0]["reason"] == "expired"
    assert bridge.counter["commands_rejected"] == 1
    assert bridge.counter["pick_executed"] == 0
    assert published_on(transport, bridge.progress_topic) == []


def test_missing_target_rejected() -> None:
    bridge, transport, _ = make_bridge()
    payload = make_command("cmd_tsk_0004", "dispatch", task_id="tsk_0004")

    transport.deliver(bridge.task_topic, payload, qos=1)

    acks = published_on(transport, bridge.ack_topic)
    assert acks[0]["accepted"] is False
    assert acks[0]["reason"] == "missing_target"
    assert bridge.counter["pick_executed"] == 0


def test_unknown_action_rejected() -> None:
    bridge, transport, _ = make_bridge()
    payload = make_command("cmd_0005", "fly")

    transport.deliver(bridge.cmd_topic, payload, qos=1)

    acks = published_on(transport, bridge.ack_topic)
    assert acks[0]["accepted"] is False
    assert acks[0]["reason"] == "unknown_action"


def test_non_dispatch_command_calls_driver() -> None:
    bridge, transport, _ = make_bridge()

    transport.deliver(
        bridge.cmd_topic,
        make_command("cmd_0006", "emergency_stop"),
        qos=1,
    )

    acks = published_on(transport, bridge.ack_topic)
    assert len(acks) == 1
    assert acks[0]["accepted"] is True
    assert acks[0]["reason"] == "emergency_stop"
    assert acks[0]["mode"] == DeviceMode.E_STOP


def test_device_mismatch_invalid() -> None:
    bridge, transport, _ = make_bridge()
    payload = make_command("cmd_0007", "pause")
    payload["device_id"] = "OTHER-ARM"

    transport.deliver(bridge.task_topic, payload, qos=1)

    assert bridge.counter["commands_invalid"] == 1
    assert published_on(transport, bridge.ack_topic) == []


def test_invalid_envelope_ignored() -> None:
    bridge, transport, _ = make_bridge()
    transport.deliver(bridge.task_topic, {"command_id": "broken"}, qos=1)

    assert bridge.counter["commands_invalid"] == 1
    assert bridge.counter["commands_processed"] == 0


def test_parse_via_device_command_from_dict() -> None:
    payload = dispatch_payload("tsk_0008")
    command = DeviceCommand.from_dict(payload)
    assert command.action == "dispatch"
    assert command.params["task_id"] == "tsk_0008"


def test_http_driver_pick_translates_response(monkeypatch) -> None:
    """The HTTP adapter must send one JSON action and parse arm fields."""
    captured: dict[str, object] = {}

    class FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

        def read(self) -> bytes:
            return self._body

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        captured["url"] = request.full_url  # type: ignore[attr-defined]
        captured["method"] = request.get_method()  # type: ignore[attr-defined]
        captured["body"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        assert timeout == 1.0
        return FakeResponse(
            json.dumps(
                {
                    "ok": True,
                    "mode": DeviceMode.IDLE,
                    "collected_weight": 0.12,
                    "review_result": "confirmed",
                    "evidence_url": "http://minio:9000/events/tasks/after.jpg",
                    "bins_after": {"foam": 0.12, "plastic": 0.0, "mixed": 0.0},
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    driver = HttpArmDriver(
        "http://arm.local:8080/control",
        timeout=1.0,
        battery=88,
        bins={"foam": 0.0, "plastic": 0.0, "mixed": 0.0},
    )

    result = driver.pick("tsk_http_01", Position(119.6531, 26.3867), 2)

    assert captured["url"] == "http://arm.local:8080/control"
    assert captured["method"] == "POST"
    assert captured["body"] == {
        "action": "pick",
        "task_id": "tsk_http_01",
        "target": {"lng": 119.6531, "lat": 26.3867},
        "priority": 2,
    }
    assert result.ok is True
    assert result.collected_weight == 0.12
    assert result.evidence_url == "http://minio:9000/events/tasks/after.jpg"
    assert result.bins_after["foam"] == 0.12


def test_driver_registry_builds_simulated_and_http() -> None:
    assert "simulated" in ARM_DRIVER_REGISTRY
    assert "http" in ARM_DRIVER_REGISTRY
    assert isinstance(
        build_arm_driver({"backend": "simulated", "simulated": {"battery": 80}}),
        SimulatedArmDriver,
    )
    assert isinstance(
        build_arm_driver(
            {
                "backend": "http",
                "http": {"endpoint": "http://arm.local:8080/arm"},
            }
        ),
        HttpArmDriver,
    )


def test_driver_registry_builds_vendor_block() -> None:
    driver = build_arm_driver(
        {
            "backend": "dobot_example",
            "vendors": {
                "dobot_example": {
                    "kind": "http",
                    "endpoint": "http://192.168.1.101:8080/dobot",
                    "timeout": 2.0,
                    "battery": 77,
                }
            },
        }
    )
    assert isinstance(driver, HttpArmDriver)
    assert driver.endpoint == "http://192.168.1.101:8080/dobot"
    assert driver.timeout == 2.0
    assert driver.battery == 77


def test_driver_registry_rejects_unknown_backend() -> None:
    try:
        build_arm_driver({"backend": "missing-arm"})
    except ValueError as exc:
        assert "missing-arm" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown backend")


def test_hiwonder_driver_pick_plays_taught_sequence(monkeypatch) -> None:
    """The Hiwonder adapter must send torque, positions, then stop."""

    class FakeBoard:
        def __init__(self) -> None:
            self.torque: list[tuple[int, bool]] = []
            self.positions: list[tuple[float, list[list[int]]]] = []
            self.stops: list[list[int]] = []
            self.reception = 0

        def enable_reception(self, enable: bool = True) -> None:
            self.reception += int(enable)

        def bus_servo_enable_torque(self, servo_id: int, enable: bool) -> None:
            self.torque.append((servo_id, enable))

        def bus_servo_set_position(
            self, duration: float, positions: list[list[int]]
        ) -> None:
            self.positions.append((duration, positions))

        def bus_servo_stop(self, servo_ids: list[int]) -> None:
            self.stops.append(list(servo_ids))

        def get_battery(self) -> None:
            return None

    board = FakeBoard()
    monkeypatch.setattr(
        HiwonderBusServoArmDriver, "_new_board", lambda self: board
    )
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    driver = HiwonderBusServoArmDriver(
        serial_port="/dev/ttyAMA0",
        baudrate=1000000,
        servo_ids=[1, 2],
        home_positions=[[1, 500], [2, 500]],
        pick_sequence=[
            {"duration": 1.0, "positions": [[1, 650], [2, 500]]},
            {"duration": 1.2, "positions": [[1, 300], [2, 600]]},
        ],
        collected_weight=0.05,
    )

    result = driver.pick("tsk_hiwonder_01", Position(119.6531, 26.3867), 1)

    assert result.ok is True
    assert result.collected_weight == 0.05
    assert result.review_result == "recheck"
    assert result.bins_after["foam"] == 0.05
    assert board.reception == 1
    assert board.torque == [(1, True), (2, True)]
    assert board.positions == [
        (1.0, [[1, 650], [2, 500]]),
        (1.2, [[1, 300], [2, 600]]),
    ]
    assert board.stops == [[1, 2]]


def test_hiwonder_driver_loads_pick_sequence_file(tmp_path) -> None:
    seq_file = tmp_path / "pick_sequence.json"
    seq_file.write_text(
        json.dumps(
            {
                "format": "oceanus_hiwonder_pick_sequence",
                "version": 1,
                "servo_ids": [1, 2],
                "steps": [
                    {"duration": 1.0, "positions": [[1, 650], [2, 500]]},
                    {"duration": 1.2, "positions": [[1, 300], [2, 600]]},
                ],
            }
        ),
        encoding="utf-8",
    )

    driver = HiwonderBusServoArmDriver(
        pick_sequence_file=str(seq_file),
        servo_ids=[1, 2],
    )

    assert driver.pick_sequence == [
        {"duration": 1.0, "positions": [[1, 650], [2, 500]]},
        {"duration": 1.2, "positions": [[1, 300], [2, 600]]},
    ]


def test_hiwonder_driver_rejects_both_sequence_sources() -> None:
    try:
        HiwonderBusServoArmDriver(
            pick_sequence=[
                {"duration": 1.0, "positions": [[1, 500]]}
            ],
            pick_sequence_file="whatever.json",
        )
    except ValueError as exc:
        assert "not both" in str(exc)
    else:
        raise AssertionError("expected ValueError for two sequence sources")


def test_teach_tool_output_loads_through_driver(tmp_path) -> None:
    from arm_bridge.tools.teach_hiwonder_sequence import build_output

    payload = build_output(
        [1, 2],
        [
            {"duration": 1.0, "positions": [[1, 650], [2, 500]]},
            {"duration": 1.0, "positions": [[1, 300], [2, 600]]},
        ],
        1.0,
    )
    out = tmp_path / "recorded.json"
    out.write_text(json.dumps(payload), encoding="utf-8")

    driver = HiwonderBusServoArmDriver(
        pick_sequence_file=str(out),
        servo_ids=[1, 2],
    )

    assert driver.pick_sequence == payload["steps"]


def test_hiwonder_driver_registry_builds_from_vendor_block() -> None:
    assert "hiwonder_bus_servo" in ARM_DRIVER_REGISTRY
    driver = build_arm_driver(
        {
            "backend": "hiwonder_bus_servo",
            "vendors": {
                "hiwonder_bus_servo": {
                    "kind": "hiwonder_bus_servo",
                    "serial_port": "/dev/ttyAMA0",
                    "baudrate": 1000000,
                    "servo_ids": [1, 2, 3, 4],
                    "home_positions": [[1, 500], [2, 500]],
                    "pick_sequence": [
                        {"duration": 1.0, "positions": [[1, 650], [2, 500]]}
                    ],
                }
            },
        }
    )
    assert isinstance(driver, HiwonderBusServoArmDriver)
    assert driver.servo_ids == [1, 2, 3, 4]


def test_hiwonder_status_without_sdk_is_deterministic(monkeypatch) -> None:
    def missing_sdk(self: HiwonderBusServoArmDriver) -> object:
        raise DriverError("ros_robot_controller_sdk is unavailable")

    monkeypatch.setattr(HiwonderBusServoArmDriver, "_new_board", missing_sdk)
    driver = HiwonderBusServoArmDriver(
        pick_sequence=[{"duration": 1.0, "positions": [[1, 500], [2, 500]]}],
        battery=90,
    )

    status = driver.status()
    assert status.mode == DeviceMode.IDLE
    assert status.battery == 90
    assert status.bins == {"foam": 0.0, "plastic": 0.0, "mixed": 0.0}

    try:
        driver.pick("tsk_hiwonder_02", Position(119.6531, 26.3867), 1)
    except DriverError:
        pass
    else:
        raise AssertionError("expected DriverError when SDK is missing")


def test_check_driver_resolves_without_calling() -> None:
    from arm_bridge.main import check_driver

    cfg = {
        "driver": {
            "backend": "dobot_example",
            "vendors": {
                "dobot_example": {
                    "kind": "http",
                    "endpoint": "http://192.168.1.101:8080/dobot",
                }
            },
        }
    }
    assert check_driver(cfg, None) == 0
    assert check_driver(cfg, "simulated") == 0
    assert check_driver(cfg, "missing-arm") == 1
