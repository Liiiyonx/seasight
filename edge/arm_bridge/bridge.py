"""MQTT contract bridge between SeaSight and any physical arm.

Responsibilities:

- Subscribe to ``robot/{id}/task`` and ``robot/{id}/cmd``.
- Acknowledge commands with the frozen ACK envelope.
- Translate arm driver results into ``task/progress`` and ``marine/...``
  telemetry messages.
- Deduplicate commands by ``command_id`` and reject expired commands.

The bridge never talks to arm-specific SDKs directly. Implement one
``ArmDriver`` adapter per arm model and the rest of the chain stays the same.

Evidence level: E1/E2. Passing self tests proves the protocol loop under a
deterministic transport, not physical pickup or field acceptance.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EDGE = HERE.parent
if str(EDGE) not in sys.path:
    sys.path.insert(0, str(EDGE))

from device_sim.protocol import (  # noqa: E402
    COMMAND_ACTIONS,
    Ack,
    Clock,
    DeviceCommand,
    DeviceMode,
    FakeClock,
    IdFactory,
    Position,
    SequentialIdFactory,
)
from device_sim.transport import DeviceTransport, MemoryTransport  # noqa: E402
from .drivers import ArmDriver, DriverError, PickResult

logger = logging.getLogger("arm_bridge")


class WallClock:
    """Real-time clock implementing the injectable ``Clock`` protocol."""

    def now(self) -> float:
        return time.time()


def _iso(epoch: float) -> str:
    return FakeClock.iso(epoch)


class ArmBridge:
    """Reusable bridge connecting one robot device to the platform."""

    def __init__(
        self,
        device_id: str,
        site_id: str,
        driver: ArmDriver,
        transport: DeviceTransport | None = None,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
    ) -> None:
        self.device_id = str(device_id)
        self.site_id = str(site_id)
        self.driver = driver
        self.transport = transport or MemoryTransport()
        self.clock = clock or WallClock()
        self.id_factory = id_factory or SequentialIdFactory("ack")

        self.task_topic = f"robot/{self.device_id}/task"
        self.cmd_topic = f"robot/{self.device_id}/cmd"
        self.ack_topic = f"robot/{self.device_id}/cmd/ack"
        self.progress_topic = f"robot/{self.device_id}/task/progress"
        self.telemetry_topic = f"marine/{self.site_id}/{self.device_id}/telemetry"
        self.status_topic = f"marine/{self.site_id}/{self.device_id}/status"

        self._started = False
        self._acked: dict[str, Ack] = {}
        self.history: dict[str, list[Any]] = {
            "commands": [],
            "acks": [],
            "progress": [],
            "telemetry": [],
        }
        self.events: list[dict[str, Any]] = []
        self.counter: dict[str, int] = {
            "commands_processed": 0,
            "commands_invalid": 0,
            "commands_rejected": 0,
            "duplicates": 0,
            "acks_published": 0,
            "progress_published": 0,
            "telemetry_published": 0,
            "pick_executed": 0,
            "pick_failed": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Subscribe to the robot command channels."""
        if self._started:
            return
        self._started = True
        self.transport.subscribe(self.task_topic, self._on_message)
        self.transport.subscribe(self.cmd_topic, self._on_message)

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self.transport.unsubscribe(self.task_topic, self._on_message)
        self.transport.unsubscribe(self.cmd_topic, self._on_message)

    def publish_online(self) -> None:
        now = self.clock.now()
        self.transport.publish(
            self.status_topic,
            {"device_id": self.device_id, "online": True, "ts": _iso(now)},
            qos=1,
        )

    def publish_offline(self) -> None:
        now = self.clock.now()
        self.transport.publish(
            self.status_topic,
            {"device_id": self.device_id, "online": False, "ts": _iso(now)},
            qos=1,
        )

    def receive_command(self, command: DeviceCommand | dict[str, Any]) -> None:
        """Inject a command directly (tests and self-test mode)."""
        if isinstance(command, dict):
            command = DeviceCommand.from_dict(command)
        self._handle_command(command, self.clock.now())

    def snapshot(self) -> dict[str, Any]:
        status = self.driver.status()
        return {
            "device_id": self.device_id,
            "site_id": self.site_id,
            "mode": status.mode,
            "counter": dict(self.counter),
            "events": list(self.events),
        }

    # ------------------------------------------------------------------
    # Inbound commands
    # ------------------------------------------------------------------
    def _on_message(self, topic: str, payload: dict[str, Any]) -> None:
        try:
            command = DeviceCommand.from_dict(payload)
        except (KeyError, TypeError, ValueError) as exc:
            self.counter["commands_invalid"] += 1
            self._event("command_invalid", topic=topic, error=str(exc))
            return
        self._handle_command(command, self.clock.now())

    def _handle_command(self, command: DeviceCommand, now: float) -> None:
        self.counter["commands_processed"] += 1
        self.history["commands"].append(command)

        if command.device_id != self.device_id:
            self.counter["commands_invalid"] += 1
            self._event(
                "command_invalid",
                command_id=command.command_id,
                reason="device_id_mismatch",
            )
            return

        previous = self._acked.get(command.command_id)
        if previous is not None:
            self.counter["duplicates"] += 1
            self._event("duplicate_command", command_id=command.command_id)
            self._publish_ack(previous)
            return

        if command.action not in COMMAND_ACTIONS:
            self._reject(command, "unknown_action", now)
            return

        if command.is_expired(now):
            self._reject(command, "expired", now)
            return

        if command.action == "dispatch":
            self._handle_dispatch(command, now)
            return

        self._handle_other(command, now)

    def _handle_dispatch(self, command: DeviceCommand, now: float) -> None:
        task_id = command.params.get("task_id")
        target = command.params.get("target")
        if not task_id or not isinstance(target, dict):
            self._reject(command, "missing_target", now)
            return
        try:
            target_pos = Position(float(target["lng"]), float(target["lat"]))
        except (KeyError, TypeError, ValueError):
            self._reject(command, "missing_target", now)
            return

        ack = self._make_ack(
            command, accepted=True, reason="dispatched", mode=DeviceMode.NAVIGATING, now=now
        )
        self._publish_ack(ack)
        self._event("dispatch_accepted", command_id=command.command_id, task_id=task_id)

        self._publish_progress(task_id, DeviceMode.COLLECTING)
        self._publish_telemetry(
            status=DeviceMode.COLLECTING,
            task_id=task_id,
            location=target_pos,
            now=now,
        )

        priority = int(command.params.get("priority", 3))
        try:
            result = self.driver.pick(task_id, target_pos, priority)
            self.counter["pick_executed"] += 1
            self._event("pick_done", command_id=command.command_id, task_id=task_id)
        except DriverError as exc:
            self.counter["pick_failed"] += 1
            self._event("pick_failed", command_id=command.command_id, error=str(exc))
            result = PickResult(
                ok=False,
                collected_weight=0.0,
                review_result="recheck",
                bins_after=dict(self.driver.status().bins),
            )

        self._publish_progress_done(task_id, result, now)
        self._publish_telemetry(
            status=DeviceMode.IDLE,
            task_id=task_id,
            location=self.driver.status().location,
            now=now,
        )

    def _handle_other(self, command: DeviceCommand, now: float) -> None:
        action = command.action
        accepted = True
        reason = {
            "pause": "paused",
            "resume": "resumed",
            "return_home": "returning",
            "emergency_stop": "emergency_stop",
            "ack": "ack_echo",
        }.get(action, "accepted")
        try:
            if action == "pause":
                self.driver.pause()
            elif action == "resume":
                self.driver.resume()
            elif action == "return_home":
                self.driver.return_home()
            elif action == "emergency_stop":
                self.driver.emergency_stop()
        except DriverError as exc:
            accepted = False
            reason = "driver_error"
            self._event("driver_error", command_id=command.command_id, error=str(exc))

        ack = self._make_ack(
            command,
            accepted=accepted,
            reason=reason,
            mode=self.driver.status().mode,
            now=now,
        )
        self._publish_ack(ack)
        self._event("command_applied", command_id=command.command_id, action=action)

    # ------------------------------------------------------------------
    # Outbound protocol messages
    # ------------------------------------------------------------------
    def _reject(self, command: DeviceCommand, reason: str, now: float) -> None:
        self.counter["commands_rejected"] += 1
        ack = self._make_ack(
            command,
            accepted=False,
            reason=reason,
            mode=self.driver.status().mode,
            now=now,
        )
        self._publish_ack(ack)
        self._event("command_rejected", command_id=command.command_id, reason=reason)

    def _make_ack(
        self,
        command: DeviceCommand,
        *,
        accepted: bool,
        reason: str,
        mode: str,
        now: float,
    ) -> Ack:
        return Ack(
            ack_id=self.id_factory.next(),
            command_id=command.command_id,
            device_id=self.device_id,
            seq=command.seq,
            received_at=now,
            accepted=accepted,
            reason=reason,
            mode=mode,
        )

    def _publish_ack(self, ack: Ack) -> None:
        self._acked[ack.command_id] = ack
        self.transport.publish(self.ack_topic, ack.to_dict(), qos=1)
        self.history["acks"].append(ack)
        self.counter["acks_published"] += 1
        self._event("ack_published", command_id=ack.command_id, accepted=ack.accepted)

    def _publish_progress(self, task_id: str, status: str) -> None:
        payload = {
            "task_id": task_id,
            "robot_id": self.device_id,
            "status": status,
            "progress": 1.0 if status == "done" else 0.5,
            "ts": _iso(self.clock.now()),
        }
        self.transport.publish(self.progress_topic, payload, qos=1)
        self.history["progress"].append(payload)
        self.counter["progress_published"] += 1
        self._event("progress_published", task_id=task_id, status=status)

    def _publish_progress_done(
        self, task_id: str, result: PickResult, now: float
    ) -> None:
        payload = {
            "task_id": task_id,
            "robot_id": self.device_id,
            "status": "done",
            "progress": 1.0,
            "collected_weight": round(float(result.collected_weight), 3),
            "review_result": result.review_result,
            "evidence_url": result.evidence_url,
            "bins_after": result.bins_after,
            "ts": _iso(now),
        }
        self.transport.publish(self.progress_topic, payload, qos=1)
        self.history["progress"].append(payload)
        self.counter["progress_published"] += 1
        self._event("progress_done", task_id=task_id, review_result=result.review_result)

    def _publish_telemetry(
        self,
        *,
        status: str,
        task_id: str | None,
        location: Position,
        now: float,
    ) -> None:
        driver_status = self.driver.status()
        payload = {
            "device_id": self.device_id,
            "device_type": "robot",
            "timestamp": _iso(now),
            "location": location.to_dict(),
            "status": status,
            "battery": driver_status.battery,
            "bins": dict(driver_status.bins),
            "task_id": task_id,
            "speed": driver_status.speed,
            "heading": driver_status.heading,
        }
        self.transport.publish(self.telemetry_topic, payload, qos=0)
        self.history["telemetry"].append(payload)
        self.counter["telemetry_published"] += 1
        self._event("telemetry_published", status=status, task_id=task_id)

    def _event(self, kind: str, **detail: Any) -> None:
        self.events.append({"kind": kind, "detail": detail})


__all__ = ["ArmBridge", "WallClock"]
