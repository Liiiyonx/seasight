"""仓储工厂（WP-16 总控集成）。

WP-10「可选仓储工厂」：无数据库引擎时返回**内存仓储**（默认行为，
离线测试不依赖数据库、不破坏确定性）；提供 SQLAlchemy engine 时返回
WP-10 持久化仓储（SqlAlchemyRunRepository / SqlAlchemyApprovalRepository）。

设计约束：
- 默认（engine=None）必须等价于直接使用内存仓储，零配置可用。
- 数据库不可用时不得破坏离线测试 —— 由上层选择是否提供 engine。
- 工厂只做选择与装配，不改变任何仓储语义（冻结协议见 repositories.py）。
- 仓储自身管理 ID/时钟（内存仓储无需注入；SQLAlchemy 仓储用 DB 序列）。
"""

from __future__ import annotations

from sqlalchemy.engine import Engine

from app.services.agents.persistent_repository import (
    SqlAlchemyApprovalRepository,
    SqlAlchemyRunRepository,
)
from app.services.agents.repositories import (
    InMemoryApprovalRepository,
    InMemoryRunRepository,
)


def create_run_repository(engine: Engine | None = None):
    """创建运行仓储。

    engine=None → InMemoryRunRepository（默认）；
    engine 提供 → SqlAlchemyRunRepository（WP-10 持久化，乐观锁/幂等由仓储保证）。
    """
    if engine is None:
        return InMemoryRunRepository()
    return SqlAlchemyRunRepository(engine=engine)


def create_approval_repository(engine: Engine | None = None):
    """创建审批仓储。

    engine=None → InMemoryApprovalRepository（默认）；
    engine 提供 → SqlAlchemyApprovalRepository（WP-10 持久化）。
    """
    if engine is None:
        return InMemoryApprovalRepository()
    return SqlAlchemyApprovalRepository(engine=engine)


def create_repositories(engine: Engine | None = None) -> tuple[object, object]:
    """成对创建 run + approval 仓储，返回 (run_repository, approval_repository)。

    供 AgentRuntime / API 装配点使用：数据库可选，默认内存。
    """
    return (
        create_run_repository(engine),
        create_approval_repository(engine),
    )
