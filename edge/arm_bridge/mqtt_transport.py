"""Real MQTT transport for the arm bridge.

This is a thin adapter over paho-mqtt that implements the same transport
contract used by tests, so the bridge itself stays transport-agnostic.
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("arm_bridge.mqtt")


class MqttTransport:
    """paho-mqtt based transport with LWT and exact-topic routing."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        client_id: str = "robot-arm-bridge",
        keepalive: int = 60,
        use_lwt: bool = True,
        will_topic: Optional[str] = None,
        transport: str = "tcp",
        path: str = "/mqtt",
    ) -> None:
        """创建 transport。

        参数
        ----
        transport
            ``"tcp"``（默认）走原生 MQTT，``"ws"`` 走 MQTT-over-WebSocket。

            ★ 为什么需要 ws（2026-10-05 路演场景实测得出）：
            云端 EMQX 的 1883 绑在 ``127.0.0.1``（只对服务器本机可见，
            这是正确的安全设计 —— ``docs/deployment.md`` 明确"对公网只开放
            80/443"）。但设备在路演现场只能通过 **HTTPS/WSS** 连公网，
            开不了 1883 端口。所以必须走 WebSocket，由 nginx 的 443 反代
            到 EMQX 的 8083。

            paho 1.x（树莓派上是 1.6.1）用 ``transport="ws"`` 传字符串，
            2.x 改成了枚举，所以两种都试一遍。
        path
            WebSocket 路径，需与 nginx 的 ``location`` 一致（默认 ``/mqtt``）。
        """
        self.host = str(host)
        self.port = int(port)
        self.username = username
        self.password = password
        self.client_id = str(client_id)
        self.keepalive = int(keepalive)
        self.use_lwt = bool(use_lwt)
        self.will_topic = will_topic
        self.transport = str(transport or "tcp").lower()
        self.path = str(path or "/mqtt")
        self.connected = False
        self._client: Any = None
        self._handlers: Dict[str, List[Any]] = {}

    def connect(self) -> None:
        import paho.mqtt.client as mqtt  # noqa: PLC0415

        # ★ WebSocket 传输的正确写法（2026-10-05 实测踩了四个坑）：
        #
        #   1. 传输名是 **"websockets"**，不是 "ws"
        #      （paho 2.x: ValueError transport must be "websockets",
        #      "tcp" or "unix", not ws）
        #   2. 要传给 **Client(transport=...)**，不是 connect(transport=...)
        #      （connect() 不接受该参数，TypeError）
        #   3. 路径默认 /mqtt，且 EMQX 5.8 **要求子协议 `mqtt`**
        #      （Sec-WebSocket-Protocol），缺了就 HTTP 400
        #   4. paho 1.6.1（树莓派）用字符串 "websockets"，2.x 也接受字符串
        client_kwargs = {"client_id": self.client_id}
        if self.transport == "ws":
            client_kwargs["transport"] = "websockets"
        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                protocol=mqtt.MQTTv5,
                **client_kwargs
            )
        except (AttributeError, ValueError):
            client = mqtt.Client(**client_kwargs)

        if self.transport == "ws":
            client.ws_set_options(path=self.path, **self._ws_opts())

        if self.username:
            client.username_pw_set(self.username, self.password or "")
        if self.use_lwt and self.will_topic:
            client.will_set(
                self.will_topic,
                json.dumps({"online": False}, ensure_ascii=False),
                qos=1,
                retain=True,
            )
        # ★ WebSocket 在 **ws_set_options()** 里配置，不是在 connect()。
        #   我第一版误把 transport= 传给 connect()，paho 2.x 直接
        #   TypeError: unexpected keyword argument 'transport'。
        #   paho 1.6.1（树莓派）同样是 ws_set_options 决定，不用传。
        if self.transport == "ws":
            client.ws_set_options(path=self.path, **self._ws_opts())

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.connect(self.host, self.port, keepalive=self.keepalive)
        client.loop_start()
        self._client = client

    @staticmethod
    def _ws_opts() -> Dict[str, Any]:
        """``ws_set_options`` 的参数。

        ★★ ``Sec-WebSocket-Protocol: mqtt`` 是**必须**的（2026-10-05 实测）。
           EMQX 5.8 的 WS 监听会校验子协议：
             - 带 ``mqtt``      → 握手成功
             - 不带 / 带别的     → **HTTP 400**（路径对也拒绝）
           paho 的 ``ws_set_options`` **不会自动发**这个头，
           所以必须手动加到 headers 里，否则表现为「连不上但看不出原因」。

        paho 1.6.1（树莓派）与 2.x 都接受 ``path`` + ``headers``。
        """
        return {"headers": {"Sec-WebSocket-Protocol": "mqtt"}}

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

    def publish(self, topic: str, payload: Dict[str, Any], qos: int = 1) -> bool:
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
