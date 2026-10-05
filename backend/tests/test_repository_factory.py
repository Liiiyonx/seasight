"""WP-16 集成：可选仓储工厂契约测试（总控串行项）。

锁定：
- 默认（engine=None）返回内存仓储 —— 离线测试零配置可用。
- 提供 engine 返回 SQLAlchemy 持久化仓储（WP-10）。
- create_repositories 成对返回 (run, approval)。
"""

from __future__ import annotations

from sqlalchemy import create_engine

from app.services.agents import (
    InMemoryApprovalRepository,
    InMemoryRunRepository,
    SqlAlchemyApprovalRepository,
    SqlAlchemyRunRepository,
    create_approval_repository,
    create_repositories,
    create_run_repository,
)


def test_factory_defaults_to_memory() -> None:
    """无引擎 → 内存仓储（离线测试默认行为）。"""
    run = create_run_repository()
    assert isinstance(run, InMemoryRunRepository)
    approval = create_approval_repository()
    assert isinstance(approval, InMemoryApprovalRepository)


def test_factory_with_engine_returns_persistent() -> None:
    """提供 SQLAlchemy engine → WP-10 持久化仓储。"""
    engine = create_engine("sqlite://")
    try:
        run = create_run_repository(engine=engine)
        assert isinstance(run, SqlAlchemyRunRepository)
        approval = create_approval_repository(engine=engine)
        assert isinstance(approval, SqlAlchemyApprovalRepository)
    finally:
        engine.dispose()


def test_create_repositories_pair() -> None:
    """成对工厂返回 (run, approval)，默认内存。"""
    run, approval = create_repositories()
    assert isinstance(run, InMemoryRunRepository)
    assert isinstance(approval, InMemoryApprovalRepository)


def test_create_repositories_with_engine() -> None:
    """成对工厂在提供引擎时返回持久化对。"""
    engine = create_engine("sqlite://")
    try:
        run, approval = create_repositories(engine=engine)
        assert isinstance(run, SqlAlchemyRunRepository)
        assert isinstance(approval, SqlAlchemyApprovalRepository)
    finally:
        engine.dispose()


def test_memory_factory_behavior_matches_direct_construction() -> None:
    """工厂的内存路径与直接构造等价（语义零偏差）。"""
    from app.services.agents.repositories import InMemoryApprovalRepository as DirectApproval
    from app.services.agents.repositories import InMemoryRunRepository as DirectRun

    assert isinstance(create_run_repository(), DirectRun)
    assert isinstance(create_approval_repository(), DirectApproval)
