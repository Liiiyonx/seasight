"""WebSocket 接口：大屏实时推送。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from app.core.config import settings
from app.core.deps import decode_access_token
from app.ws.manager import ws_manager

router = APIRouter()


@router.websocket("/ws/alerts")
async def ws_alerts(websocket: WebSocket) -> None:
    """实时告警推送通道。

    消息契约的真源是 `app/ws/manager.py` 的 `WS_MESSAGE_CONTRACT`，
    字段清单见 docs/api.md §服务端推送消息。

    服务端会发的全部 6 种消息：

    - connected     ：连接建立后立即发送（本函数直接发，走 ws 而非广播）
    - new_event     ：新识别事件（大屏标红告警）
    - task_update   ：任务状态变更（工单看板刷新）
    - robot_status  ：机器人遥测（地图标记刷新）
    - pong          ：对客户端 {"type":"ping"} 的应答
    - heartbeat     ：60 秒无消息时主动探测存活

    心跳：客户端每 25 秒发 {"type":"ping"}，服务端回 pong；
    服务端 60 秒收不到任何消息则主动发 heartbeat 探测。
    """
    token = websocket.query_params.get("token")
    user = decode_access_token(token)

    # 浏览器 WebSocket 不能设置 Authorization 头，因此用 query token。
    # 开发环境保留匿名连接以便浏览器验收；生产环境必须拒绝无效令牌，
    # 否则公网任何人都能持续读取实时告警流。
    if settings.app_env == "production" and user is None:
        logger.warning(
            f"[WS] 拒绝未认证连接 client={websocket.client.host if websocket.client else '-'}"
        )
        await websocket.close(code=4401, reason="authentication required")
        return

    await ws_manager.connect(websocket)
    try:
        # 先发一条欢迎消息，确认通道可用
        await websocket.send_json(
            {
                "type": "connected",
                "data": {"message": "实时告警通道已建立"},
            }
        )

        while True:
            try:
                # 等待客户端消息（含心跳），超时 60 秒
                message = await asyncio.wait_for(websocket.receive_json(), timeout=60)
                if isinstance(message, dict) and message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                # 超时发服务端心跳，探测连接是否存活
                await websocket.send_json({"type": "heartbeat"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:   # noqa: BLE001
        logger.warning(f"[WS] 连接异常：{exc}")
    finally:
        await ws_manager.disconnect(websocket)
