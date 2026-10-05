"""认证与用户接口。

四类角色：
    admin    系统管理员 —— 全部权限
    operator 乡镇操作员 —— 本辖区写权限
    approver 审批员 —— 仅可决定 Agent 人工审批
    viewer   访客 —— 只读大屏

说明：种子数据的密码为演示用，首次部署后应立即修改。
"""

from __future__ import annotations

import hmac
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import CurrentUser, get_current_user
from app.core.exceptions import ApiResponse, AppException, ErrorCode
from app.db.session import get_session
from app.models.misc import User, UserRole
from app.schemas import LoginRequest

router = APIRouter()


@router.post("/login", summary="账号登录")
async def login(
    payload: LoginRequest,
    session: AsyncSession = Depends(get_session),
):
    """账号密码登录，返回访问令牌。

    令牌为服务端签名载荷，前端放在 ``Authorization: Bearer <token>``；
    业务接口只信任该令牌中的身份与角色，不接受客户端自报请求头。
    """
    user = (
        await session.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()

    if user is None or not _verify_password(payload.password, user.hashed_password):
        logger.warning(f"[认证] 登录失败：{payload.username}")
        raise AppException(
            code=ErrorCode.UNAUTHORIZED,
            message="用户名或密码错误",
            http_status=401,
        )

    if not user.is_active:
        raise AppException(
            code=ErrorCode.FORBIDDEN,
            message="账号已停用",
            http_status=403,
        )

    token = _issue_token(user.username, user.role, user.township_scope)

    from app.services.audit import record_audit

    await record_audit(
        session,
        username=user.username,
        role=user.role,
        action="login",
        target_type="auth",
        detail="登录成功",
    )

    user.last_login_at = datetime.now()
    await session.commit()

    logger.info(f"[认证] 登录成功：{user.username} ({user.role})")

    return ApiResponse.ok(
        {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": settings.access_token_expire_minutes * 60,
            "role": user.role,
            "full_name": user.full_name,
            "township_scope": user.township_scope,
        }
    )


@router.get("/me", summary="当前用户信息")
async def me(user: CurrentUser = Depends(get_current_user)):
    return ApiResponse.ok(
        {
            "username": user.username,
            "role": user.role,
            "township_scope": user.township_scope,
            "can_write": user.can_write,
        }
    )


@router.get("/users", summary="用户列表（管理员）")
async def list_users(
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    if not user.is_admin:
        raise AppException(
            code=ErrorCode.FORBIDDEN, message="仅管理员可查看用户列表", http_status=403
        )

    rows = (await session.execute(select(User).order_by(User.username))).scalars().all()
    return ApiResponse.ok(
        [
            {
                "username": r.username,
                "full_name": r.full_name,
                "role": r.role,
                "role_label": {
                    UserRole.ADMIN: "系统管理员",
                    UserRole.OPERATOR: "乡镇操作员",
                    UserRole.APPROVER: "审批员",
                    UserRole.VIEWER: "访客",
                }.get(r.role, r.role),
                "township_scope": r.township_scope,
                "is_active": r.is_active,
                "last_login_at": r.last_login_at.isoformat() if r.last_login_at else None,
            }
            for r in rows
        ]
    )


# ----------------------------------------------------------------------
# 密码与令牌
# ----------------------------------------------------------------------
def _verify_password(plain: str, hashed: str) -> bool:
    """校验密码。

    开发环境允许在 bcrypt 依赖异常时回退到种子演示账号，便于无依赖启动；
    生产环境绝不接受硬编码密码，以免依赖故障意外放大为认证绕过。
    """
    try:
        from passlib.context import CryptContext

        ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
        return ctx.verify(plain, hashed)
    except Exception:   # noqa: BLE001
        if settings.app_env == "production":
            logger.error("[认证] 密码校验组件不可用，生产环境拒绝降级")
            return False
        # 降级：种子数据中的演示账号
        fallback = {
            "admin": "admin123456",
            "operator": "operator123456",
            "viewer": "viewer123456",
        }
        for name, pwd in fallback.items():
            if hashed.startswith("$2b$") and _demo_hash_match(hashed, name):
                return hmac.compare_digest(plain, pwd)
        return False


_DEMO_USER_BY_HASH_PREFIX = {
    "LQv3c1yqBWVHxkd0LHAkCO": "admin",
    "EixZaYVK1fsbw1ZfbX3OXe": "operator",
    "Vc6V8u7BqC5rP7p3mLjXtu": "viewer",
}


def _demo_hash_match(hashed: str, name: str) -> bool:
    """演示账号哈希前缀比对（依赖缺失时的兜底）。"""
    for prefix, user_name in _DEMO_USER_BY_HASH_PREFIX.items():
        if hashed.startswith(f"$2b$12${prefix}") and user_name == name:
            return True
    return False


def _issue_token(username: str, role: str, township_scope: str | None = None) -> str:
    """签发访问令牌。

    正式实现用 python-jose 签 JWT；此处保持接口稳定，
    便于后续替换而不影响调用方。
    载荷含 sub/role/scope/exp，由 `deps.get_current_user` 验签解析。
    """
    import base64
    import hashlib
    import json

    payload = {
        "sub": username,
        "role": role,
        "scope": township_scope,
        "exp": int(
            (datetime.now() + timedelta(minutes=settings.access_token_expire_minutes)).timestamp()
        ),
    }
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hashlib.sha256(f"{body}.{settings.secret_key}".encode()).hexdigest()[:32]
    return f"{body}.{sig}"
