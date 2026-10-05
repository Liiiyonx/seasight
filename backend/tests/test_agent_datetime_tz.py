"""WP-16 集成：时钟、仓储与数据库时间混用回归测试。

PG 仓储读回 TIMESTAMPTZ 为 aware（SQLite 不暴露此差异）；FakeClock/SystemClock
当前均返回 aware UTC；runtime 仍用 _as_aware 兼容历史 naive 值（naive 视为
UTC），仓储排序也必须对 None/naive/aware 混用保持安全。
scratch PG 三场景验证暴露该缺陷后锁定。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.agents import (
    AgentRuntime,
    FakeClock,
    InMemoryApprovalRepository,
    InMemoryMemoryStore,
    InMemoryRunRepository,
    RuntimeConfig,
    ToolRegistry,
)
from app.services.agents.model import AgentRun, AgentRunRequest
from app.services.agents.repositories import ApprovalRecord
from app.services.agents.runtime import _as_aware


def _runtime() -> AgentRuntime:
    return AgentRuntime(
        tool_registry=ToolRegistry(),
        run_repository=InMemoryRunRepository(),
        approval_repository=InMemoryApprovalRepository(),
        memory_store=InMemoryMemoryStore(),
        clock=FakeClock(),
        runtime_config=RuntimeConfig(),
    )


def _run(run_id: str) -> AgentRun:
    return AgentRun(
        run_id=run_id,
        trigger_type="event",
        objective="处理海漂垃圾事件并完成派单闭环",
        status="waiting_approval",
        request=AgentRunRequest(
            trigger_type="event",
            objective="处理海漂垃圾事件并完成派单闭环",
            actor="operator-01",
            role="operator",
            params={"event_id": "evt_001"},
        ),
    )


def test_as_aware_unifies_naive_and_aware() -> None:
    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    naive = datetime(2026, 1, 1)
    assert _as_aware(aware).tzinfo is not None
    assert _as_aware(naive).tzinfo is not None
    assert _as_aware(aware) - _as_aware(naive) == timedelta(0)


def test_resolve_approvals_naive_clock_aware_record() -> None:
    """审批记录 requested_at 为 aware（模拟 PG 读回），时钟 naive —— 不得抛 TypeError。"""
    rt = _runtime()
    run = _run("run_tz1")
    run.runtime_state["approvals"] = ["apr_tz1"]
    rt.runs.create(run)
    rt.approvals.create(
        ApprovalRecord(
            approval_id="apr_tz1",
            run_id="run_tz1",
            requested_action="dispatch",
            risk_level="sensitive",
            requested_by="operator-01",
            requested_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
            decision=None,
        )
    )
    assert rt._resolve_approvals(run) == "pending"


def test_resolve_approvals_naive_clock_default_record() -> None:
    """内存默认路径（requested_at 由仓储补 naive 值）行为不变。"""
    rt = _runtime()
    run = _run("run_tz2")
    run.runtime_state["approvals"] = ["apr_tz2"]
    rt.runs.create(run)
    rt.approvals.create(
        ApprovalRecord(
            approval_id="apr_tz2",
            run_id="run_tz2",
            requested_action="dispatch",
            risk_level="sensitive",
            requested_by="operator-01",
            decision=None,
        )
    )
    assert rt._resolve_approvals(run) == "pending"


def test_sweep_expired_naive_clock_aware_started_at() -> None:
    """run.started_at 为 aware（PG 读回）时 _sweep_expired 不抛 TypeError。"""
    rt = _runtime()
    run = _run("run_tz3")
    run.status = "executing"
    run.started_at = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    rt.runs.create(run)
    rt._sweep_expired()  # 不应抛异常


def test_repository_sort_handles_none_naive_and_aware_times() -> None:
    """仓储列表排序不得因 None/naive/aware 混用抛 TypeError。"""
    runs = InMemoryRunRepository()
    for run_id, created_at in (
        ("run_aware", datetime(2026, 1, 1, 1, tzinfo=timezone.utc)),
        ("run_naive", datetime(2026, 1, 1, 2)),
        ("run_none", None),
    ):
        runs.create(
            AgentRun(
                run_id=run_id,
                trigger_type="event",
                objective=run_id,
                status="succeeded",
                request=AgentRunRequest(
                    trigger_type="event",
                    objective=run_id,
                    actor="operator-01",
                    role="operator",
                ),
                created_at=created_at,
            )
        )
    assert [run.run_id for run in runs.list()] == [
        "run_none",
        "run_aware",
        "run_naive",
    ]

    approvals = InMemoryApprovalRepository()
    for approval_id, requested_at in (
        ("apr_aware", datetime(2026, 1, 1, 1, tzinfo=timezone.utc)),
        ("apr_naive", datetime(2026, 1, 1, 2)),
        ("apr_none", None),
    ):
        approvals.create(
            ApprovalRecord(
                approval_id=approval_id,
                run_id="run_001",
                requested_action="dispatch",
                risk_level="sensitive",
                requested_by="operator-01",
                requested_at=requested_at,
            )
        )
    assert [record.approval_id for record in approvals.list_all()] == [
        "apr_none",
        "apr_aware",
        "apr_naive",
    ]
