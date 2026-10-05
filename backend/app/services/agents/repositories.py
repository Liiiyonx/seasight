"""运行与审批仓储：接口（可注入）+ 内存实现。

★ 唯一约束（手册 3.7）在内存实现中强制执行：
    - run_id / step_id / approval_id 唯一
    - t_agent_step 的 (run_id, step_no) 唯一
    - 幂等键 → run_id 索引唯一（重复触发复用 run）
字段名与 WP-02 ORM 对齐：ApprovalRecord 对齐 t_agent_approval，
AgentRun / AgentStep 对齐 t_agent_run / t_agent_step。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from app.services.agents.errors import AgentNotFoundError, TaskConflictError
from app.services.agents.model import AgentRun, AgentStep


def _time_key(value: datetime | None) -> datetime:
    """统一排序时间：None 视为最早，naive 按 UTC，aware 转 UTC。"""
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass
class ApprovalRecord:
    """一条人工审批记录（字段对齐 t_agent_approval）。

    decision：None=待审批 / "approved"=通过 / "rejected"=拒绝 /
    "cancelled"=随 run 取消。
    """

    approval_id: str
    run_id: str
    requested_action: str
    risk_level: str  # RiskLevel 的值
    requested_by: str
    decided_by: str | None = None
    decision: str | None = None
    reason: str | None = None
    requested_at: datetime | None = None
    decided_at: datetime | None = None


# ----------------------------------------------------------------------
# Run 仓储
# ----------------------------------------------------------------------


class RunRepository(Protocol):
    """运行仓储协议（可注入，见手册 3.6）。

    协议方法体做参数校验后抛 NotImplementedError：既保证形参被真实
    使用（静态守卫），又保持纯协议语义 —— 具体行为由注入的实现提供。
    """

    def create(self, run: AgentRun) -> None:
        if not isinstance(run, AgentRun):
            raise TypeError(f"run 必须是 AgentRun：{type(run).__name__}")
        raise NotImplementedError("RunRepository.create 未实现")

    def save(self, run: AgentRun) -> None:
        if not isinstance(run, AgentRun):
            raise TypeError(f"run 必须是 AgentRun：{type(run).__name__}")
        raise NotImplementedError("RunRepository.save 未实现")

    def get(self, run_id: str) -> AgentRun | None:
        if not isinstance(run_id, str):
            raise TypeError(f"run_id 必须是 str：{type(run_id).__name__}")
        raise NotImplementedError("RunRepository.get 未实现")

    def list(self, *, status: str | None = None, limit: int | None = None) -> list[AgentRun]:
        if status is not None and not isinstance(status, str):
            raise TypeError(f"status 必须是 str 或 None：{type(status).__name__}")
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
            raise TypeError(f"limit 必须是非负整数或 None：{limit!r}")
        raise NotImplementedError("RunRepository.list 未实现")

    def find_by_idempotency_key(self, key: str) -> AgentRun | None:
        if not isinstance(key, str) or not key:
            raise TypeError(f"key 必须是非空 str：{key!r}")
        raise NotImplementedError("RunRepository.find_by_idempotency_key 未实现")

    def add_step(self, step: AgentStep) -> None:
        if not isinstance(step, AgentStep):
            raise TypeError(f"step 必须是 AgentStep：{type(step).__name__}")
        raise NotImplementedError("RunRepository.add_step 未实现")

    def steps(self, run_id: str) -> list[AgentStep]:
        if not isinstance(run_id, str):
            raise TypeError(f"run_id 必须是 str：{type(run_id).__name__}")
        raise NotImplementedError("RunRepository.steps 未实现")

    def count(self) -> int:
        raise NotImplementedError("RunRepository.count 未实现")


class InMemoryRunRepository:
    """内存运行仓储。"""

    def __init__(self) -> None:
        self._runs: dict[str, AgentRun] = {}
        self._steps: dict[str, list[AgentStep]] = {}
        self._step_keys: set[tuple[str, int]] = set()
        self._step_ids: set[str] = set()
        self._by_idem: dict[str, str] = {}

    def create(self, run: AgentRun) -> None:
        if run.run_id in self._runs:
            raise TaskConflictError(f"run_id 重复：{run.run_id}")
        self._runs[run.run_id] = run
        key = run.request.idempotency_key if run.request is not None else None
        if key:
            if key in self._by_idem:
                raise TaskConflictError(f"幂等键重复：{key}")
            self._by_idem[key] = run.run_id

    def save(self, run: AgentRun) -> None:
        if run.run_id not in self._runs:
            raise AgentNotFoundError(f"run 不存在：{run.run_id}")
        self._runs[run.run_id] = run

    def get(self, run_id: str) -> AgentRun | None:
        return self._runs.get(run_id)

    def list(self, *, status: str | None = None, limit: int | None = None) -> list[AgentRun]:
        runs = sorted(self._runs.values(), key=lambda r: _time_key(r.created_at))
        if status is not None:
            runs = [r for r in runs if r.status == status]
        if limit is not None:
            runs = runs[:limit]
        return runs

    def find_by_idempotency_key(self, key: str) -> AgentRun | None:
        run_id = self._by_idem.get(key)
        if run_id is None:
            return None
        return self._runs.get(run_id)

    def add_step(self, step: AgentStep) -> None:
        if step.step_id in self._step_ids:
            raise TaskConflictError(f"step_id 重复：{step.step_id}")
        if (step.run_id, step.step_no) in self._step_keys:
            raise TaskConflictError(f"(run_id, step_no) 重复：{step.run_id} #{step.step_no}")
        self._steps.setdefault(step.run_id, []).append(step)
        self._step_keys.add((step.run_id, step.step_no))
        self._step_ids.add(step.step_id)

    def steps(self, run_id: str) -> list[AgentStep]:
        steps = sorted(self._steps.get(run_id, []), key=lambda s: s.step_no)
        return list(steps)

    def count(self) -> int:
        return len(self._runs)


# ----------------------------------------------------------------------
# 审批仓储
# ----------------------------------------------------------------------


class ApprovalRepository(Protocol):
    """审批仓储协议（可注入，见手册 3.6）。

    协议方法体做参数校验后抛 NotImplementedError：既保证形参被真实
    使用（静态守卫），又保持纯协议语义 —— 具体行为由注入的实现提供。
    """

    def create(self, record: ApprovalRecord) -> ApprovalRecord:
        if not isinstance(record, ApprovalRecord):
            raise TypeError(f"record 必须是 ApprovalRecord：{type(record).__name__}")
        raise NotImplementedError("ApprovalRepository.create 未实现")

    def get(self, approval_id: str) -> ApprovalRecord | None:
        if not isinstance(approval_id, str):
            raise TypeError(f"approval_id 必须是 str：{type(approval_id).__name__}")
        raise NotImplementedError("ApprovalRepository.get 未实现")

    def list_pending(self, run_id: str | None = None) -> list[ApprovalRecord]:
        if run_id is not None and not isinstance(run_id, str):
            raise TypeError(f"run_id 必须是 str 或 None：{type(run_id).__name__}")
        raise NotImplementedError("ApprovalRepository.list_pending 未实现")

    def list_all(self) -> list[ApprovalRecord]:
        """列出全部审批记录（含已决策），按请求时间升序。"""
        raise NotImplementedError("ApprovalRepository.list_all 未实现")

    def decide(
        self,
        approval_id: str,
        decided_by: str,
        decision: str,
        reason: str | None = None,
    ) -> ApprovalRecord:
        if not isinstance(approval_id, str):
            raise TypeError(f"approval_id 必须是 str：{type(approval_id).__name__}")
        if not isinstance(decided_by, str):
            raise TypeError(f"decided_by 必须是 str：{type(decided_by).__name__}")
        if decision not in ("approved", "rejected", "cancelled"):
            raise ValueError(f"非法决策：{decision!r}")
        if reason is not None and not isinstance(reason, str):
            raise TypeError(f"reason 必须是 str 或 None：{type(reason).__name__}")
        raise NotImplementedError("ApprovalRepository.decide 未实现")

    def count(self) -> int:
        raise NotImplementedError("ApprovalRepository.count 未实现")


class InMemoryApprovalRepository:
    """内存审批仓储。"""

    def __init__(self) -> None:
        self._records: dict[str, ApprovalRecord] = {}

    def create(self, record: ApprovalRecord) -> ApprovalRecord:
        if record.approval_id in self._records:
            raise TaskConflictError(f"approval_id 重复：{record.approval_id}")
        self._records[record.approval_id] = record
        return record

    def get(self, approval_id: str) -> ApprovalRecord | None:
        return self._records.get(approval_id)

    def list_pending(self, run_id: str | None = None) -> list[ApprovalRecord]:
        records = [r for r in self._records.values() if r.decision is None]
        if run_id is not None:
            records = [r for r in records if r.run_id == run_id]
        records.sort(key=lambda r: _time_key(r.requested_at))
        return records

    def list_all(self) -> list[ApprovalRecord]:
        records = sorted(
            self._records.values(),
            key=lambda r: _time_key(r.requested_at),
        )
        return list(records)

    def decide(
        self,
        approval_id: str,
        decided_by: str,
        decision: str,
        reason: str | None = None,
    ) -> ApprovalRecord:
        record = self._records.get(approval_id)
        if record is None:
            raise AgentNotFoundError(f"审批不存在：{approval_id}")
        if record.decision is not None and record.decision != "cancelled":
            # 已决策的审批不允许被覆盖（cancelled 允许被覆盖为正式决策）
            raise TaskConflictError(f"审批 {approval_id} 已决策：{record.decision}")
        if decision not in ("approved", "rejected", "cancelled"):
            raise AgentNotFoundError(f"非法审批决策：{decision}")
        record.decision = decision
        record.decided_by = decided_by
        record.reason = reason
        record.decided_at = datetime.now(timezone.utc)
        return record

    def count(self) -> int:
        return len(self._records)
