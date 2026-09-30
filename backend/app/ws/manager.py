"""WebSocket 连接管理器：大屏实时推送。

设计：单进程内存广播（备赛规模足够）。
多实例部署时需换成 Redis Pub/Sub 广播，届时替换 broadcast 实现即可。

★ 消息契约集中在这里声明
────────────────────────
推送负载是**手工拼的 dict**，散落在 handlers / tasks / consumer 多处。
手工拼装必然漂移：曾出现同一份 `task_update` 在四个推送点里，
有一处漏了 `event_id`，而前端用 `{...data}` 展开后静默拿到 `undefined`
—— 不报错，只是数据看起来对、实际缺一块。

故把契约（消息类型 + 每个类型必须携带的字段）提取为常量，
并提供 `validate_payload()` 供测试穷举校验，见
`backend/tests/test_ws_contract.py`。
契约真源：docs/api.md §「服务端推送消息」表。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import WebSocket
from loguru import logger

from app.core.config import settings

# ============================================================
# 消息契约（真源：docs/api.md §服务端推送消息）
# ============================================================
# 每个消息类型必须携带的字段。▲ 表示该字段允许为 None，但键必须存在。
WS_MESSAGE_CONTRACT: dict[str, tuple[str, ...]] = {
    # 新事件入库（大屏标红告警）
    "new_event": (
        "event_id",
        "device_id",
        "main_class",
        "lng",
        "lat",
        "det_count",
        "confidence",
        "event_time",
    ),
    # 任务状态变更（工单看板刷新）
    "task_update": ("task_id", "event_id", "robot_id", "status"),
    # 机器人遥测上报（地图标记刷新）
    "robot_status": (
        "robot_id",
        "battery",
        "status",
        "lng",
        "lat",
        "task_id",
        "bins",
        "heading",
        "speed",
    ),
    # 连接建立后立即发送
    "connected": ("message",),
    # 客户端 ping 的应答（无 data）
    "pong": (),
    # 服务端 60 秒无消息时主动探测（无 data）
    "heartbeat": (),
}

# 允许出现在负载里、但契约未强制要求的附加字段（各推送点按需附带）
WS_OPTIONAL_FIELDS: frozenset[str] = frozenset(
    {"finished_at", "started_at", "assigned_at", "ack_at",
     "collected_weight", "review_result", "priority", "remark"}
)


def validate_payload(message_type: str, data: dict[str, Any]) -> list[str]:
    """校验一次推送的负载是否符合契约。

    返回问题清单（空列表 = 合格）。**不抛异常** —— 由调用方
    （当前是测试）决定怎么处理，避免运行期因为契约问题阻断推送。

    只校验「该有的有没有」，不校验「多余的可不可以」，
    因为各推送点按需附带额外字段是允许的（见 WS_OPTIONAL_FIELDS）。
    """
    problems: list[str] = []

    if message_type not in WS_MESSAGE_CONTRACT:
        return [f"未在契约中声明的消息类型：{message_type}"]

    required = WS_MESSAGE_CONTRACT[message_type]
    for field in required:
        if field not in data:
            problems.append(f"{message_type} 缺少必带字段 `{field}`")

    return problems


class ConnectionManager:
    """WebSocket 连接池 + 广播。"""

    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        logger.info(f"[WS] 新连接，当前在线 {len(self._connections)}")

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)
        logger.info(f"[WS] 断开连接，当前在线 {len(self._connections)}")

    async def broadcast(self, message_type: str, data: dict[str, Any]) -> None:
        """向所有连接广播消息。

        type: new_event | task_update | robot_status | alert

        ★ 发之前先自检负载：契约声明必带的字段若缺失，记 warning 留痕。
          不阻断推送（大屏少一个字段也好过整条消息不发），但**必须留痕** ——
          这类缺陷的恶劣之处就在于静默。
        """
        problems = validate_payload(message_type, data)
        for p in problems:
            logger.warning(f"[WS] 推送负载不符契约：{p}（type={message_type}）")

        if not self._connections:
            return

        payload = {
            "type": message_type,
            "data": data,
            "ts": datetime.now().isoformat(),
        }

        dead: list[WebSocket] = []
        for ws in list(self._connections):
            try:
                await ws.send_json(payload)
            except Exception:   # noqa: BLE001
                dead.append(ws)

        # 清理失活连接
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.discard(ws)

    async def push_new_event(self, event_data: dict[str, Any]) -> None:
        """推送新事件告警（大屏标红用）。"""
        await self.broadcast("new_event", event_data)

    async def push_task_update(self, task_data: dict[str, Any]) -> None:
        """推送任务状态变更（工单看板用）。"""
        await self.broadcast("task_update", task_data)

    async def push_robot_status(self, robot_data: dict[str, Any]) -> None:
        """推送机器人状态（地图标记刷新用）。"""
        await self.broadcast("robot_status", robot_data)

    @property
    def connection_count(self) -> int:
        return len(self._connections)


# 全局单例（由 main.py 在 lifespan 中初始化并使用）
ws_manager = ConnectionManager()
