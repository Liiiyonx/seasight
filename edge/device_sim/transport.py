"""WP-14 设备孪生 —— 传输层（可注入）。

传输层只定义**协议接口**和**内存实现**：

- :class:`DeviceTransport` 是设备孪生依赖的抽象：连接状态、发布、
  订阅。测试与场景注入**内存 transport**，绝不依赖真实 MQTT / broker。
- :class:`MemoryTransport` 是一个进程内发布-订阅总线：维护连接状态、
  按主题路由到订阅者，并记录所有已投递 / 已发布的报文，供断言使用。

语义约定（与 `docs/device-interface.md` 一致）：

- ``connected=False`` 时，``publish`` 直接返回 False，**不投递**。
  断网期间的报文由设备侧 Outbox 排队、重连后按原顺序补传
  （排队/补传逻辑在 `device.py`，不在传输层）。
- ``connected=False`` 时，``deliver``（平台 → 设备的入站命令）同样
  被丢弃 —— 离线设备收不到指令，符合真实弱网语义。

证据等级：E1/E2 —— 内存 transport 只证明孪生协议与闭环在确定性环境
下可复现，不代表真实 MQTT 或真实边缘盒接入。
"""

from __future__ import annotations

from collections import deque
from typing import Any, Callable, Protocol

#: 订阅回调签名：收到 (topic, payload_dict)
MessageHandler = Callable[[str, dict[str, Any]], None]


class DeviceTransport(Protocol):
    """设备孪生依赖的传输抽象（可注入）。"""

    @property
    def connected(self) -> bool: ...

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def publish(self, topic: str, payload: dict[str, Any], qos: int = 1) -> bool: ...

    def subscribe(self, topic: str, handler: MessageHandler) -> None: ...

    def unsubscribe(self, topic: str, handler: MessageHandler) -> None: ...


class MemoryTransport:
    """进程内内存 transport —— 测试与确定性场景的默认注入对象。

    公开只读记录：

    - ``published``：按时间顺序记录每次 ``publish`` 调用
      （``(topic, payload_dict, qos)``）。
    - ``delivered``：按时间顺序记录每条实际被路由到订阅者的报文
      （``(topic, payload_dict, qos)``）。断网期间不产生 delivered。
    """

    def __init__(self) -> None:
        self.connected = True
        self._handlers: dict[str, list[MessageHandler]] = {}
        self.published: list[tuple[str, dict[str, Any], int]] = []
        self.delivered: list[tuple[str, dict[str, Any], int]] = []

    # ---------------- 连接状态 ----------------
    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    # ---------------- 发布 ----------------
    def publish(self, topic: str, payload: dict[str, Any], qos: int = 1) -> bool:
        """设备 → 平台方向。未连接时返回 False 且不投递。"""
        self.published.append((topic, payload, qos))
        if not self.connected:
            return False
        self._route(topic, payload, qos)
        self.delivered.append((topic, payload, qos))
        return True

    # ---------------- 订阅 ----------------
    def subscribe(self, topic: str, handler: MessageHandler) -> None:
        self._handlers.setdefault(topic, []).append(handler)

    def unsubscribe(self, topic: str, handler: MessageHandler) -> None:
        handlers = self._handlers.get(topic)
        if handlers is None:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            pass
        if not handlers:
            self._handlers.pop(topic, None)

    # ---------------- 入站注入（平台 → 设备） ----------------
    def deliver(self, topic: str, payload: dict[str, Any], qos: int = 1) -> bool:
        """把一条入站报文（平台下发的命令）注入总线，路由给订阅者。

        设备离线（``connected=False``）时入站被丢弃，返回 False。
        """
        if not self.connected:
            return False
        self._route(topic, payload, qos)
        self.delivered.append((topic, payload, qos))
        return True

    # ---------------- 内部 ----------------
    def _route(self, topic: str, payload: dict[str, Any], qos: int) -> None:
        for handler in list(self._handlers.get(topic, ())):
            handler(topic, payload)

    # ---------------- 只读快照 ----------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "published": [(t, p, q) for t, p, q in self.published],
            "delivered": [(t, p, q) for t, p, q in self.delivered],
        }

    def reset(self) -> None:
        self.connected = True
        self._handlers.clear()
        self.published.clear()
        self.delivered.clear()


class RecordingTransport:
    """便捷包装：把订阅到的报文按主题归档，供测试直接读取。

    例如场景 runner 用它收集 ``robot/{id}/telemetry`` 与
    ``robot/{id}/cmd/ack`` 的全部报文。
    """

    def __init__(self, inner: DeviceTransport) -> None:
        self.inner = inner
        self.by_topic: dict[str, list[dict[str, Any]]] = {}

    def subscribe(self, topic: str, handler: MessageHandler) -> None:
        def wrapper(t: str, payload: dict[str, Any]) -> None:
            self.by_topic.setdefault(t, []).append(payload)
            handler(t, payload)

        self.inner.subscribe(topic, wrapper)

    def unsubscribe(self, topic: str, handler: MessageHandler) -> None:
        self.inner.unsubscribe(topic, handler)

    def __getattr__(self, name: str) -> Any:  # 委托其余接口
        return getattr(self.inner, name)


__all__ = ["DeviceTransport", "MemoryTransport", "RecordingTransport"]
