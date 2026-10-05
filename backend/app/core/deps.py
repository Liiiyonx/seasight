"""FastAPI 依赖注入。

集中管理可复用的依赖：数据库会话、Redis 客户端、当前用户等。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Annotated, AsyncGenerator

import redis.asyncio as aioredis
from fastapi import Depends, Header
from redis.asyncio import Redis

from app.core.config import settings
from app.core.exceptions import AppException, ErrorCode

# ----------------------------------------------------------------------
# Redis 客户端（懒加载单例）
# ----------------------------------------------------------------------
_redis_client: Redis | None = None


async def get_redis() -> Redis:
    """获取 Redis 客户端（全局复用连接池）。

    ★ protocol=2：redis-py 5.x 默认发 `HELLO` 做 RESP3 握手，
    但老版本 Redis（如 Windows 的 3.0.x）不认识该命令会报
    `unknown command 'HELLO'`。强制 RESP2 规避。
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
            protocol=2,
        )
    return _redis_client


async def get_redis_dep() -> AsyncGenerator[Redis, None]:
    """FastAPI 依赖：Redis 客户端。"""
    yield await get_redis()


async def close_redis() -> None:
    """应用关闭时释放 Redis 连接。"""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None


# ----------------------------------------------------------------------
# 当前用户（简化版：从 Header 读角色；生产环境换成 JWT 校验）
# ----------------------------------------------------------------------
class CurrentUser:
    """当前登录用户上下文。"""

    def __init__(self, username: str, role: str, township_scope: str | None = None) -> None:
        self.username = username
        self.role = role
        self.township_scope = township_scope

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_write(self) -> bool:
        return self.role in ("admin", "operator")


def decode_access_token(token: str | None) -> CurrentUser | None:
    """严格验签并解析访问令牌；缺失、过期、伪造时返回 ``None``。

    HTTP 接口可通过 ``get_current_user`` 将无效令牌降级为匿名只读；
    WebSocket 在生产环境必须直接拒绝 ``None``，不能沿用匿名连接。
    """
    if not token:
        return None

    try:
        body, sig = token.rsplit(".", 1)
        expected = hashlib.sha256(f"{body}.{settings.secret_key}".encode()).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected):
            raise ValueError("签名不匹配")
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("令牌已过期")
        username = payload.get("sub", "anonymous")
        role = payload.get("role", "viewer")
        scope = payload.get("scope")
        # approver 只用于人工审批；写权限仍由 can_write 单独限制为
        # admin/operator，不能让审批角色意外获得工单写权限。
        if role not in ("admin", "operator", "approver", "viewer"):
            raise ValueError("角色无效")
    except Exception:   # noqa: BLE001
        return None

    return CurrentUser(username, role, scope)


async def get_current_user(
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> CurrentUser:
    """获取当前用户 —— 解析签名令牌，未登录回退匿名只读。

    ★ 为什么不能从 X-User / X-Role 头读身份：
    那是**前端自己填的**，用户改一下 localStorage 就能伪造角色
    （把自己改成 admin）——「看起来有权限」其实是「没权限」。
    真实权限必须来自**服务端签名**的令牌：角色写进签名载荷，
    前端无法篡改，只能拿着登录时签发的令牌证明身份。

    令牌格式见 `auth._issue_token`：`base64url(payload).sha256(body+secret)[:32]`。
    """
    token = None
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()

    if not token:
        # 未登录 → 匿名只读：大屏演示可直接看，但写操作会被 require_operator 拒绝
        return CurrentUser("anonymous", "viewer", None)

    user = decode_access_token(token)
    if user is None:
        # 令牌无效（伪造 / 过期 / 篡改）→ 视为匿名，不抛 401（演示友好，正式可收紧）
        return CurrentUser("anonymous", "viewer", None)

    return user


async def require_operator(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    """要求操作员及以上权限。"""
    if not user.can_write:
        raise AppException(
            code=ErrorCode.FORBIDDEN,
            message="当前账号无写权限（需要 operator 或 admin）",
            http_status=403,
        )
    return user
