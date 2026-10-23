"""审计日志接口（管理员）。

政务交付的可追溯性：管理员可回溯「谁在何时对什么做了什么」。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user
from app.core.exceptions import ApiResponse, AppException, ErrorCode
from app.db.session import get_session
from app.models.misc import AuditLog

router = APIRouter()

ACTION_LABELS = {
    "login": "登录",
    "task_create": "人工建单",
    "task_status_update": "更新工单状态",
    "event_status_update": "更新事件状态",
}


@router.get("", response_model=ApiResponse[dict], summary="审计日志（管理员）")
async def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    username: str | None = Query(None),
    action: str | None = Query(None),
    user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """分页查询操作审计日志，仅管理员可访问。"""
    if not user.is_admin:
        raise AppException(
            code=ErrorCode.FORBIDDEN, message="仅管理员可查看审计日志", http_status=403
        )

    conditions = []
    if username:
        conditions.append(AuditLog.username == username)
    if action:
        conditions.append(AuditLog.action == action)

    count_stmt = select(func.count()).select_from(AuditLog)
    stmt = select(AuditLog)
    if conditions:
        count_stmt = count_stmt.where(and_(*conditions))
        stmt = stmt.where(and_(*conditions))

    total = int((await session.execute(count_stmt)).scalar_one())
    rows = (
        await session.execute(
            stmt.order_by(AuditLog.created_at.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).scalars().all()

    items = [
        {
            "id": r.id,
            "username": r.username,
            "role": r.role,
            "action": r.action,
            "action_label": ACTION_LABELS.get(r.action, r.action),
            "target_type": r.target_type,
            "target_id": r.target_id,
            "detail": r.detail,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
    return ApiResponse.ok(
        {"items": items, "meta": {"total": total, "page": page, "page_size": page_size}}
    )
