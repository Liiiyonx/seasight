"""Real MQTT transport for the arm bridge.

This is a thin adapter over paho-mqtt that implements the same transport
contract used by tests, so the bridge itself stays transport-agnostic.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("arm_bridge.mqtt")


class MqttTransport:
    """paho-mqtt based transport with LWT and exact-topic routing."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        client_id: str = "robot-arm-bridge",
        keepalive: int = 60,
        use_lwt: bool = True,
        will_topic: str | None = None,
    ) -> None:
        self.host = str(host)
        self.port = int(port)
        self.username = username
        self.password = password
        self.client_id = str(client_id)
        self.keepalive = int(keepalive)
        self.use_lwt = bool(use_lwt)
        self.will_topic = will_topic
        self.connected = False
        self._client: Any = None
        self._handlers: dict[str, list[Any]] = {}

    def connect(self) -> None:
        import paho.mqtt.client as mqtt  # noqa: PLC0415

        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=self.client_id,
                protocol=mqtt.MQTTv5,
            )
        except (AttributeError, ValueError):
            client = mqtt.Client(client_id=self.client_id)

        if self.username:
            client.username_pw_set(self.username, self.password or "")
        if self.use_lwt and self.will_topic:
            client.will_set(
                self.will_topic,
                json.dumps({"online": False}, ensure_ascii=False),
                qos=1,
                retain=True,
            )
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.connect(self.host, self.port, keepalive=self.keepalive)
        client.loop_start()
        self._client = client

    def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:  # noqa: BLE001
            logger.warning("MQTT disconnect failed", exc_info=True)
        self._client = None
        self.connected = False

    def subscribe(self, topic: str, handler: Any) -> None:
        self._handlers.setdefault(topic, []).append(handler)
        if self.connected and self._client is not None:
            self._client.subscribe(topic, qos=1)

    def unsubscribe(self, topic: str, handler: Any) -> None:
        handlers = self._handlers.get(topic)
        if not handlers:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            pass
        if not handlers:
            self._handlers.pop(topic, None)
            if self.connected and self._client is not None:
                self._client.unsubscribe(topic)

    def publish(self, topic: str, payload: dict[str, Any], qos: int = 1) -> bool:
        if not self.connected or self._client is None:
            return False
        info = self._client.publish(
            topic, json.dumps(payload, ensure_ascii=False), qos=qos, retain=False
        )
        return getattr(info, "rc", 0) == 0

    def _on_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
        self.connected = True
        for topic, handlers in self._handlers.items():
            client.subscribe(topic, qos=1)
        logger.info("arm bridge connected: %s", self.host)

    def _on_disconnect(self, *args: Any) -> None:
        self.connected = False
        logger.warning("arm bridge disconnected from %s", self.host)

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        topic = str(message.topic)
        try:
            payload = json.loads(message.payload) if message.payload else {}
        except ValueError:
            logger.warning("invalid JSON on %s", topic)
            return
        for handler in list(self._handlers.get(topic, ())):
            handler(topic, payload)
