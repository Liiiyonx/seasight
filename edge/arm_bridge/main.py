#!/usr/bin/env python
"""Run the arm bridge against a real broker or as a no-hardware self test.

Examples:

    # No broker, no hardware: inject one dispatch and print the full loop.
    python edge/arm_bridge/main.py --dry-run

    # Verify that a named driver resolves from the registry + config.
    python edge/arm_bridge/main.py --check-driver --driver dobot_example

    # Real broker with the configured driver (simulated or HTTP).
    python edge/arm_bridge/main.py --config edge/arm_bridge/config.yaml

The self test proves the MQTT contract only (E1/E2). It is not physical
pickup evidence and does not imply detection accuracy.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EDGE = HERE.parent
if str(EDGE) not in sys.path:
    sys.path.insert(0, str(EDGE))

import yaml  # noqa: E402

from arm_bridge.bridge import ArmBridge  # noqa: E402
from arm_bridge.drivers import build_arm_driver  # noqa: E402
from arm_bridge.mqtt_transport import MqttTransport  # noqa: E402
from device_sim.protocol import FakeClock  # noqa: E402
from device_sim.transport import MemoryTransport  # noqa: E402

DEFAULT_CONFIG = HERE / "config.yaml"


def _load_config(path: Path) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def build_driver(cfg: dict[str, Any], backend: str | None) -> Any:
    driver_cfg = cfg.get("driver", {})
    return build_arm_driver(driver_cfg, backend)


def run_self_test(cfg: dict[str, Any]) -> int:
    """Run one dispatch through an in-memory transport and exit."""
    # Deliberately simulated: the self test proves the MQTT contract without
    # touching a physical arm or a vendor HTTP endpoint. Driver resolution is
    # covered separately by --check-driver.
    transport = MemoryTransport()
    bridge = ArmBridge(
        device_id=str(cfg["device_id"]),
        site_id=str(cfg["site_id"]),
        driver=build_driver(cfg, "simulated"),
        transport=transport,
    )
    bridge.start()

    now = time.time()
    payload = {
        "command_id": "cmd_tsk_selftest_0001",
        "device_id": cfg["device_id"],
        "seq": 1,
        "action": "dispatch",
        "issued_at": now,
        "issued_at_iso": FakeClock.iso(now),
        "expires_at": now + 30.0,
        "expires_at_iso": FakeClock.iso(now + 30.0),
        "params": {
            "task_id": "tsk_selftest_0001",
            "target": {"lng": 119.6531, "lat": 26.3867},
            "priority": 1,
        },
    }
    transport.deliver(bridge.task_topic, payload, qos=1)

    print("[self-test] command accepted")
    for topic, body, qos in transport.published:
        print(f"[self-test] {topic} qos={qos}")
        print(f"            {body}")

    statuses = [p["status"] for p in bridge.history["progress"]]
    if statuses != ["collecting", "done"]:
        print(f"[self-test] FAIL: unexpected progress sequence {statuses}", file=sys.stderr)
        return 1
    if bridge.counter["pick_executed"] != 1:
        print("[self-test] FAIL: driver pick did not execute", file=sys.stderr)
        return 1
    print("[self-test] OK: assigned -> navigating(ack) -> collecting -> done")
    return 0


def check_driver(cfg: dict[str, Any], backend: str | None) -> int:
    """Build the named driver from registry + config without calling it."""
    try:
        driver = build_driver(cfg, backend)
    except Exception as exc:  # noqa: BLE001
        print(f"[check-driver] FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"[check-driver] OK: backend={backend or cfg.get('driver', {}).get('backend', 'simulated')} -> {type(driver).__name__}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--dry-run", action="store_true", help="no broker; run self test")
    parser.add_argument("--self-test", action="store_true", help="alias for --dry-run")
    parser.add_argument(
        "--check-driver",
        action="store_true",
        help="resolve driver from registry + config, without calling it",
    )
    parser.add_argument("--driver", default=None, help="override driver.backend")
    parser.add_argument("--device-id", default=None)
    parser.add_argument("--site-id", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = _load_config(Path(args.config))

    if args.check_driver:
        return check_driver(cfg, args.driver)

    if args.dry_run or args.self_test:
        return run_self_test(cfg)

    device_id = args.device_id or str(cfg["device_id"])
    site_id = args.site_id or str(cfg["site_id"])
    mqtt_cfg = cfg.get("mqtt", {})

    transport = MqttTransport(
        host=str(mqtt_cfg.get("host", "localhost")),
        port=int(mqtt_cfg.get("port", 1883)),
        username=mqtt_cfg.get("username"),
        password=mqtt_cfg.get("password"),
        client_id=f"robot-{device_id}",
        keepalive=int(mqtt_cfg.get("keepalive", 60)),
        use_lwt=bool(mqtt_cfg.get("use_lwt", True)),
        will_topic=f"marine/{site_id}/{device_id}/status",
    )
    bridge = ArmBridge(
        device_id=device_id,
        site_id=site_id,
        driver=build_driver(cfg, args.driver),
        transport=transport,
    )
    bridge.start()

    try:
        transport.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"[error] cannot connect MQTT broker: {exc}", file=sys.stderr)
        print("        use --dry-run to run the self test without a broker", file=sys.stderr)
        return 2

    bridge.publish_online()
    print(f"[arm-bridge] {device_id} at {site_id} connected, waiting for tasks")

    stop = False

    def _stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while not stop:
        time.sleep(1.0)

    bridge.publish_offline()
    transport.disconnect()
    print("[arm-bridge] stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
