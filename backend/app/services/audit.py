"""操作审计 —— 记录关键写操作，支撑政务交付的可追溯性。

背景（产品完整性四象限「缺失」项）：政务场景里「这个工单是谁改成 done 的」
是审计刚需。没有审计日志，任何写操作都无从回溯 —— 而这恰恰是
「平台是中枢」应该提供的可追溯性。

设计：`record_audit` 只做 `session.add`，**不 commit、不 await 外部 IO**，
由调用方在业务事务里一起 commit —— 审计与业务同生共死，
不会出现「业务成功了但审计丢了」或「审计成功但业务回滚了」的分叉。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.misc import AuditLog


async def record_audit(
    session: AsyncSession,
    *,
    username: str,
    role: str,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: str | None = None,
) -> None:
    """追加一条审计记录（随调用方事务一起提交）。"""
    session.add(
        AuditLog(
            username=username,
            role=role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
        )
    )
