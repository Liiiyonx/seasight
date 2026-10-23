"""Real MQTT transport for the arm bridge.

This is a thin adapter over paho-mqtt that implements the same transport
contract used by tests, so the bridge itself stays transport-agnostic.
"""

from __future__ import annotations

import json
import logging
import os
import time
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
        protocol: str | None = None,
        connect_timeout: float = 5.0,
    ) -> None:
        self.host = str(host)
        self.port = int(port)
        self.username = username
        self.password = password
        self.client_id = str(client_id)
        self.keepalive = int(keepalive)
        self.use_lwt = bool(use_lwt)
        self.will_topic = will_topic
        #: 等 CONNACK 的上限（秒）。见 connect() 里的说明。
        self.connect_timeout = float(
            os.environ.get("OCEANUS_MQTT_CONNECT_TIMEOUT", connect_timeout)
        )
        #: MQTT 协议版本。生产是 EMQX（支持 v5），默认 v5 不变；但并非所有 broker
        #: 都支持 v5 —— 本机联调用的临时 broker（amqtt）只支持 3.1.1，用
        #: ``OCEANUS_MQTT_PROTOCOL=311`` 切换即可，与 host/port/账号那组 env 覆盖
        #: 同一个思路（同一份 config.yaml 部署到不同机器）。
        self.protocol = str(
            protocol if protocol is not None else os.environ.get("OCEANUS_MQTT_PROTOCOL", "5")
        ).lower()
        self.connected = False
        self.last_error: str | None = None
        self._client: Any = None
        self._handlers: dict[str, list[Any]] = {}

    def connect(self) -> None:
        import paho.mqtt.client as mqtt  # noqa: PLC0415

        use_v5 = self.protocol in ("5", "v5", "mqtt5", "mqttv5")
        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=self.client_id,
                protocol=mqtt.MQTTv5 if use_v5 else mqtt.MQTTv311,
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

        # ★ 必须等 CONNACK 再返回：paho 的 connect() 只完成 TCP 握手，连接结果
        #   由 on_connect 异步回调给出。不等的话，紧随其后的 publish_online()
        #   会因为 self.connected 仍是 False 被**静默丢弃** —— 平台侧永远看不到
        #   这台机械臂上线（retained 状态也一直是旧的）。等不到就明确报错，
        #   让 main.py 走"连不上 broker"的失败出口，而不是假装起来了。
        deadline = time.time() + max(0.5, self.connect_timeout)
        while time.time() < deadline and not self.connected:
            if self.last_error:
                raise RuntimeError(self.last_error)
            time.sleep(0.05)
        if not self.connected:
            raise RuntimeError(
                f"MQTT 连接超时：{self.host}:{self.port} 在 {self.connect_timeout:.0f}s 内"
                f"没有返回 CONNACK（broker 未就绪 / 端口不对 / 被 ACL 拦）"
            )

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
        # paho v2 回调：3.1.1 与 v5 都把结果放在 reason_code 里（v5 是 ReasonCode）
        rc = getattr(reason_code, "value", reason_code)
        if rc not in (0, None):
            self.last_error = f"broker 拒绝连接：{reason_code}"
            logger.error("arm bridge MQTT connect refused: %s", reason_code)
            return
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
