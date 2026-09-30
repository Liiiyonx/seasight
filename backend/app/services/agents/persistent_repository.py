"""SqlAlchemy 持久化仓储（WP-10）：内存默认、数据库可选。

实现现有 `RunRepository` / `ApprovalRepository` 协议（repositories.py），
**不修改协议语义**；内存仓储保持默认、数据库不可用时离线测试不受影响。

★ 使用同步 SQLAlchemy Engine + 独立 Session（供已有的同步 Runtime 调用）：
   - Engine 与 Session 工厂可注入：测试注入 SQLite 内存（StaticPool 共享
     单连接，保证「跨仓储实例」可见），生产注入 PostgreSQL 同步驱动。
   - 所有写操作在单事务内完成；冲突回滚并保留原数据（不静默覆盖）。
   - 幂等键唯一部分索引（t_agent_run_state.idempotency_key）保证：
     相同幂等键并发创建时最多产生一个 run。
   - state_version 乐观并发：保存 run 时库内版本与调用方观察版本不一致，
     抛现有 `TaskConflictError`（不静默覆盖）；成功保存 +1。
   - 步骤写入保持 (run_id, step_no) 唯一约束，不覆盖历史步骤。
   - 审批创建/查询/决策全部落 t_agent_approval，跨进程/跨仓储实例可见，
     已决策的审批不允许被覆盖（cancelled 允许被正式决策覆盖，同内存语义）。
   - close() 释放连接池，避免测试泄漏连接。

★ 续跑状态持久化的关键设计（runtime.py 为冻结文件，不得修改）：
   AgentRuntime 只在终态调用 save()；中间状态（stage / status / 审批挂起）
   的内存变更无法被 save() 捕获。因此本仓储在**每次仓储写入**时对已知的
   run 对象做一次「检查点快照」：把当前 status + request + runtime_state
   同步到 t_agent_run / t_agent_run_state（不递增 state_version，last-write-wins
   崩溃恢复语义），并在 `steps()` 读取路径上同样刷新 —— 这样 `run()` 返回
   前 `_result()` 的 `steps()` 调用能捕获「waiting_approval」等 stage 翻转，
   重启后 resume() 从数据库精确续跑，不重放规划/策略/审批阶段。

★ 解密预算：request_json / runtime_state_json 写入前统一 `_sanitize`：
   丢弃敏感键（api_key / authorization / chain_of_thought / reasoning /
   password / secret / credential / *_token 等，键名也不得进入持久化内容），
   字符串值中的敏感词替换为 [REDACTED]，长值截断为摘要长度。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models.agent import (
    AgentApproval as AgentApprovalORM,
    AgentRun as AgentRunORM,
    AgentStep as AgentStepORM,
)
from app.models.agent_state import AgentRunState as AgentRunStateORM
from app.services.agents.errors import AgentNotFoundError, TaskConflictError
from app.services.agents.model import AgentRun, AgentRunRequest, AgentStep, Expectation, RuleStep
from app.services.agents.repositories import ApprovalRecord

logger = logging.getLogger("seasight.agents.persistent")

# ----------------------------------------------------------------------
# 脱敏（解密预算）—— 键名命中即整体丢弃，字符串值中的敏感词替换
# ----------------------------------------------------------------------

_FORBIDDEN_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|chain[_-]?of[_-]?thought|reasoning|"
    r"private[_-]?reasoning|raw[_-]?reasoning|full[_-]?input|full[_-]?output|"
    r"password|passwd|secret|credential|access[_-]?token|auth[_-]?token)"
)

# 敏感字段探测：测试 8 断言持久化内容不得出现这些字面量
SENSITIVE_TERMS: tuple[str, ...] = (
    "api_key",
    "authorization",
    "chain_of_thought",
    "reasoning",
)

_MAX_STRING_LEN = 500  # 摘要长度上限：只存续跑所需业务摘要


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    """递归脱敏：敏感键丢弃、敏感词替换、长字符串截断。"""
    if depth > 8:
        return "..."
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _FORBIDDEN_KEY_RE.search(key):
                continue  # 敏感键整体丢弃（键名也不得进入持久化内容）
            out[key] = _sanitize(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        cleaned = _FORBIDDEN_KEY_RE.sub("[REDACTED]", value)
        return cleaned if len(cleaned) <= _MAX_STRING_LEN else cleaned[:_MAX_STRING_LEN] + "..."
    return value


def _json_default(value: Any) -> str:
    """json.dumps 兜底：datetime / Decimal / set 等转为可序列化形式。"""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(str(v) for v in value)
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


# ----------------------------------------------------------------------
# AgentRunRequest <-> JSON（脱敏后）
# ----------------------------------------------------------------------


def _serialize_request(request: AgentRunRequest | None) -> str:
    if request is None:
        return "{}"
    payload = {
        "trigger_type": request.trigger_type,
        "objective": request.objective,
        "actor": request.actor,
        "role": request.role,
        "params": request.params or {},
        "idempotency_key": request.idempotency_key,
        "trace_id": request.trace_id,
        "plan_key": request.plan_key,
        # 经验检索上下文：续跑/回放时要能还原"当时内核知道哪些业务事实"，
        # 否则同一条 run 重放会给出不同的经验引用。
        "lessons_context": request.lessons_context or {},
    }
    return json.dumps(_sanitize(payload), ensure_ascii=False, sort_keys=True, default=_json_default)


def _deserialize_request(raw: str | None) -> AgentRunRequest | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return AgentRunRequest(
        trigger_type=str(data.get("trigger_type") or "event"),
        objective=str(data.get("objective") or ""),
        actor=str(data.get("actor") or "operator"),
        role=str(data.get("role") or "operator"),
        params=dict(data.get("params") or {}),
        idempotency_key=data.get("idempotency_key"),
        trace_id=data.get("trace_id"),
        plan_key=data.get("plan_key"),
        lessons_context=dict(data.get("lessons_context") or {}),
    )


# ----------------------------------------------------------------------
# runtime_state <-> JSON（只保留续跑所需字段，脱敏后）
# ----------------------------------------------------------------------

# 续跑/回放字段：阶段、计划游标、重规划计数、挂起审批 ID、业务任务结果、
# 步骤角色归属（step_no → 角色）。step_roles 不参与续跑判断，但必须跟着
# 状态快照一起落库，否则服务重启后「只读回放」的轨迹会丢掉角色徽标。
_CONTINUATION_KEYS: tuple[str, ...] = (
    "stage",
    "plan_idx",
    "replan_count",
    "approvals",
    "task_result",
    "step_roles",
)


def _expectation_to_dict(expect: Expectation) -> dict[str, Any]:
    return {
        "key": expect.key,
        "min_items": expect.min_items,
        "contains": list(expect.contains),
        "on_violation": expect.on_violation,
        "error_code": expect.error_code,
    }


def _expectation_from_dict(data: dict[str, Any]) -> Expectation:
    return Expectation(
        key=str(data.get("key") or ""),
        min_items=data.get("min_items"),
        contains=tuple(data.get("contains") or ()),
        on_violation=str(data.get("on_violation") or "terminate"),
        error_code=str(data.get("error_code") or "tool_failed"),
    )


def _rule_step_to_dict(step: RuleStep) -> dict[str, Any]:
    return {
        "tool": step.tool,
        "input": step.input,
        "summary": step.summary,
        "expect": _expectation_to_dict(step.expect) if step.expect is not None else None,
        "on_error": step.on_error,
        "role": step.role,
    }


def _rule_step_from_dict(data: dict[str, Any]) -> RuleStep:
    expect_raw = data.get("expect")
    expect = None
    if isinstance(expect_raw, dict):
        expect = _expectation_from_dict(expect_raw)
    role = data.get("role")
    return RuleStep(
        tool=str(data.get("tool") or ""),
        input=dict(data.get("input") or {}),
        summary=str(data.get("summary") or ""),
        expect=expect,
        on_error=str(data.get("on_error") or "terminate"),
        role=str(role) if role else None,
    )


def _serialize_runtime_state(state: dict[str, Any]) -> str:
    payload: dict[str, Any] = {}
    for key in _CONTINUATION_KEYS:
        if key in state:
            payload[key] = state[key]
    plan = state.get("plan")
    if isinstance(plan, list):
        payload["plan"] = [_rule_step_to_dict(s) for s in plan if isinstance(s, RuleStep)]
    bindings = state.get("bindings")
    if isinstance(bindings, dict):
        payload["bindings"] = bindings
    return json.dumps(_sanitize(payload), ensure_ascii=False, sort_keys=True, default=_json_default)


def _deserialize_runtime_state(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    state: dict[str, Any] = {}
    for key in _CONTINUATION_KEYS:
        if key in data:
            state[key] = data[key]
    plan = data.get("plan")
    if isinstance(plan, list):
        state["plan"] = [_rule_step_from_dict(p) for p in plan if isinstance(p, dict)]
    bindings = data.get("bindings")
    if isinstance(bindings, dict):
        state["bindings"] = bindings
    # 兼容：老数据可能缺默认游标字段
    if "plan" in state and "plan_idx" not in state:
        state["plan_idx"] = 0
    if "replan_count" not in state:
        state["replan_count"] = 0
    if "stage" not in state:
        state["stage"] = "created"
    return state


# ----------------------------------------------------------------------
# ORM 行 <-> 领域对象
# ----------------------------------------------------------------------


def _row_to_run(run_row: AgentRunORM, state_row: AgentRunStateORM | None) -> AgentRun:
    return AgentRun(
        run_id=run_row.run_id,
        trigger_type=run_row.trigger_type,
        objective=run_row.objective,
        status=run_row.status,
        request=_deserialize_request(state_row.request_json) if state_row is not None else None,
        policy_version=run_row.policy_version,
        started_at=run_row.started_at,
        finished_at=run_row.finished_at,
        termination_reason=run_row.termination_reason,
        trace_id=run_row.trace_id,
        created_at=run_row.created_at,
        updated_at=run_row.updated_at,
        runtime_state=_deserialize_runtime_state(state_row.runtime_state_json)
        if state_row is not None
        else {},
    )


def _apply_run_row(row: AgentRunORM, run: AgentRun) -> None:
    """把领域 run 的当前状态写回 t_agent_run 行（create/save/快照共用）。"""
    row.trigger_type = run.trigger_type
    row.objective = run.objective
    row.status = run.status
    row.policy_version = run.policy_version
    row.started_at = run.started_at
    row.finished_at = run.finished_at
    row.termination_reason = run.termination_reason
    row.trace_id = run.trace_id
    row.created_at = run.created_at
    row.updated_at = run.updated_at


def _row_to_step(row: AgentStepORM) -> AgentStep:
    return AgentStep(
        step_id=row.step_id,
        run_id=row.run_id,
        step_no=row.step_no,
        step_type=row.step_type,
        decision_summary=row.decision_summary,
        tool_name=row.tool_name,
        tool_version=row.tool_version,
        input_hash=row.input_hash,
        output_hash=row.output_hash,
        status=row.status,
        latency_ms=row.latency_ms,
        error_code=row.error_code,
        created_at=row.created_at,
    )


def _row_to_approval(row: AgentApprovalORM) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row.approval_id,
        run_id=row.run_id,
        requested_action=row.requested_action,
        risk_level=row.risk_level,
        requested_by=row.requested_by,
        decided_by=row.decided_by,
        decision=row.decision,
        reason=row.reason,
        requested_at=row.requested_at,
        decided_at=row.decided_at,
    )


# ----------------------------------------------------------------------
# Run 仓储（实现 RunRepository 协议）
# ----------------------------------------------------------------------


class SqlAlchemyRunRepository:
    """同步 SQLAlchemy 运行仓储（协议：repositories.RunRepository）。

    Engine 与 Session 工厂可注入；所有写操作在事务内完成，冲突回滚并
    保留原数据。`close()` 释放连接池。
    """

    def __init__(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self._engine = engine
        self._factory = session_factory or sessionmaker(bind=engine, expire_on_commit=False)
        # 本实例观察到的 state_version（乐观并发检查的依据）
        self._versions: dict[str, int] = {}
        # 本实例接触过的 run 对象（用于中间状态检查点快照）
        self._live: dict[str, AgentRun] = {}

    # ------------------------------------------------------------------
    # 资源释放
    # ------------------------------------------------------------------

    def close(self) -> None:
        """释放连接池（测试防泄漏；重复调用安全）。"""
        self._engine.dispose()

    def _session(self) -> Session:
        return self._factory()

    # ------------------------------------------------------------------
    # 检查点快照：把 run 的当前内存状态同步到库（不递增 state_version）
    # ------------------------------------------------------------------

    def _flush_snapshot(self, run: AgentRun | None) -> None:
        """把 run 当前 status + request + runtime_state 同步到库（崩溃恢复用）。

        不参与乐观并发：last-write-wins；失败只告警不抛出（读取路径触发）。
        """
        if run is None:
            return
        try:
            with self._session() as session:
                run_row = session.execute(
                    select(AgentRunORM).where(AgentRunORM.run_id == run.run_id)
                ).scalar_one_or_none()
                state_row = session.execute(
                    select(AgentRunStateORM).where(AgentRunStateORM.run_id == run.run_id)
                ).scalar_one_or_none()
                if run_row is None or state_row is None:
                    return
                _apply_run_row(run_row, run)
                state_row.request_json = _serialize_request(run.request)
                state_row.runtime_state_json = _serialize_runtime_state(run.runtime_state)
                session.commit()
        except Exception:  # noqa: BLE001 —— 快照失败不影响主流程
            logger.warning("[AgentPersist] run %s 状态快照写入失败（忽略）", run.run_id, exc_info=True)

    # ------------------------------------------------------------------
    # RunRepository 协议方法
    # ------------------------------------------------------------------

    def create(self, run: AgentRun) -> None:
        if not isinstance(run, AgentRun):
            raise TypeError(f"run 必须是 AgentRun：{type(run).__name__}")
        with self._session() as session:
            try:
                session.add(
                    AgentRunORM(
                        run_id=run.run_id,
                        trigger_type=run.trigger_type,
                        objective=run.objective,
                        status=run.status,
                        policy_version=run.policy_version,
                        started_at=run.started_at,
                        finished_at=run.finished_at,
                        termination_reason=run.termination_reason,
                        trace_id=run.trace_id,
                        created_at=run.created_at,
                        updated_at=run.updated_at,
                    )
                )
                session.add(
                    AgentRunStateORM(
                        run_id=run.run_id,
                        request_json=_serialize_request(run.request),
                        runtime_state_json=_serialize_runtime_state(run.runtime_state),
                        idempotency_key=run.request.idempotency_key if run.request is not None else None,
                        state_version=1,
                    )
                )
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                self._raise_create_conflict(run, exc)
            else:
                self._versions[run.run_id] = 1
                self._live[run.run_id] = run

    def save(self, run: AgentRun) -> None:
        if not isinstance(run, AgentRun):
            raise TypeError(f"run 必须是 AgentRun：{type(run).__name__}")
        with self._session() as session:
            state_row = session.execute(
                select(AgentRunStateORM).where(AgentRunStateORM.run_id == run.run_id)
            ).scalar_one_or_none()
            if state_row is None:
                session.rollback()
                raise AgentNotFoundError(f"run 不存在：{run.run_id}")
            expected = self._versions.get(run.run_id)
            if expected is None:
                # 本实例从未观察过该 run（如新建实例直接保存）→ 以库内当前值为准
                expected = state_row.state_version
            if state_row.state_version != expected:
                session.rollback()
                raise TaskConflictError(
                    f"run {run.run_id} 状态版本冲突：期望 v{expected}，库内 v{state_row.state_version}"
                )
            new_version = state_row.state_version + 1
            state_row.request_json = _serialize_request(run.request)
            state_row.runtime_state_json = _serialize_runtime_state(run.runtime_state)
            state_row.state_version = new_version
            run_row = session.execute(
                select(AgentRunORM).where(AgentRunORM.run_id == run.run_id)
            ).scalar_one_or_none()
            if run_row is None:
                session.rollback()
                raise AgentNotFoundError(f"run 不存在：{run.run_id}")
            _apply_run_row(run_row, run)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise TaskConflictError(f"run {run.run_id} 保存冲突") from exc
            else:
                self._versions[run.run_id] = new_version
                self._live[run.run_id] = run

    def get(self, run_id: str) -> AgentRun | None:
        if not isinstance(run_id, str):
            raise TypeError(f"run_id 必须是 str：{type(run_id).__name__}")
        # 读取路径顺带刷新本实例已知的检查点快照（见模块 docstring）
        self._flush_snapshot(self._live.get(run_id))
        with self._session() as session:
            run_row = session.execute(
                select(AgentRunORM).where(AgentRunORM.run_id == run_id)
            ).scalar_one_or_none()
            if run_row is None:
                return None
            state_row = session.execute(
                select(AgentRunStateORM).where(AgentRunStateORM.run_id == run_id)
            ).scalar_one_or_none()
            if state_row is not None:
                self._versions[run_id] = state_row.state_version
            run = _row_to_run(run_row, state_row)
            self._live[run_id] = run
            return run

    def list(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[AgentRun]:
        if status is not None and not isinstance(status, str):
            raise TypeError(f"status 必须是 str 或 None：{type(status).__name__}")
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
            raise TypeError(f"limit 必须是非负整数或 None：{limit!r}")
        with self._session() as session:
            stmt = (
                select(AgentRunORM, AgentRunStateORM)
                .outerjoin(AgentRunStateORM, AgentRunStateORM.run_id == AgentRunORM.run_id)
                .order_by(AgentRunORM.created_at)
            )
            if status is not None:
                stmt = stmt.where(AgentRunORM.status == status)
            if limit is not None:
                stmt = stmt.limit(limit)
            runs: list[AgentRun] = []
            for run_row, state_row in session.execute(stmt).all():
                if state_row is not None:
                    self._versions[run_row.run_id] = state_row.state_version
                run = _row_to_run(run_row, state_row)
                self._live[run_row.run_id] = run
                runs.append(run)
            return runs

    def find_by_idempotency_key(self, key: str) -> AgentRun | None:
        if not isinstance(key, str) or not key:
            raise TypeError(f"key 必须是非空 str：{key!r}")
        with self._session() as session:
            state_row = session.execute(
                select(AgentRunStateORM).where(AgentRunStateORM.idempotency_key == key)
            ).scalar_one_or_none()
            if state_row is None:
                return None
            run_row = session.execute(
                select(AgentRunORM).where(AgentRunORM.run_id == state_row.run_id)
            ).scalar_one_or_none()
            if run_row is None:
                return None
            self._versions[run_row.run_id] = state_row.state_version
            run = _row_to_run(run_row, state_row)
            self._live[run_row.run_id] = run
            return run

    def add_step(self, step: AgentStep) -> None:
        if not isinstance(step, AgentStep):
            raise TypeError(f"step 必须是 AgentStep：{type(step).__name__}")
        # 步骤写入前刷新该 run 的检查点快照（捕获 stage 翻转，见模块 docstring）
        self._flush_snapshot(self._live.get(step.run_id))
        with self._session() as session:
            session.add(
                AgentStepORM(
                    step_id=step.step_id,
                    run_id=step.run_id,
                    step_no=step.step_no,
                    step_type=step.step_type,
                    decision_summary=step.decision_summary,
                    tool_name=step.tool_name,
                    tool_version=step.tool_version,
                    input_hash=step.input_hash,
                    output_hash=step.output_hash,
                    status=step.status,
                    latency_ms=step.latency_ms,
                    error_code=step.error_code,
                    created_at=step.created_at,
                )
            )
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise TaskConflictError(
                    f"步骤写入冲突（(run_id, step_no) 或 step_id 重复）："
                    f"{step.run_id} #{step.step_no}（{step.step_id}）"
                ) from exc

    def steps(self, run_id: str) -> list[AgentStep]:
        if not isinstance(run_id, str):
            raise TypeError(f"run_id 必须是 str：{type(run_id).__name__}")
        # 读取路径顺带刷新本实例已知的检查点快照（捕获终态前的 stage 翻转）
        self._flush_snapshot(self._live.get(run_id))
        with self._session() as session:
            rows = session.execute(
                select(AgentStepORM)
                .where(AgentStepORM.run_id == run_id)
                .order_by(AgentStepORM.step_no)
            ).scalars().all()
            return [_row_to_step(row) for row in rows]

    def count(self) -> int:
        with self._session() as session:
            return int(session.execute(select(func.count()).select_from(AgentRunORM)).scalar_one())

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _raise_create_conflict(self, run: AgentRun, exc: IntegrityError) -> None:
        """区分 run_id 重复与幂等键重复，统一抛 TaskConflictError。"""
        key = run.request.idempotency_key if run.request is not None else None
        if key:
            existing = self.find_by_idempotency_key(key)
            if existing is not None and existing.run_id != run.run_id:
                raise TaskConflictError(f"幂等键重复：{key}（已绑定 run {existing.run_id}）") from exc
        raise TaskConflictError(f"run_id 重复：{run.run_id}") from exc


# ----------------------------------------------------------------------
# 审批仓储（实现 ApprovalRepository 协议）
# ----------------------------------------------------------------------


class SqlAlchemyApprovalRepository:
    """同步 SQLAlchemy 审批仓储（协议：repositories.ApprovalRepository）。

    审批创建/查询/决策持久化到 t_agent_approval，跨进程/跨仓储实例可见；
    已决策的审批不允许被覆盖（cancelled 允许被正式决策覆盖，同内存语义）。
    """

    def __init__(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self._engine = engine
        self._factory = session_factory or sessionmaker(bind=engine, expire_on_commit=False)

    def close(self) -> None:
        """释放连接池（测试防泄漏；重复调用安全）。"""
        self._engine.dispose()

    def _session(self) -> Session:
        return self._factory()

    def create(self, record: ApprovalRecord) -> ApprovalRecord:
        if not isinstance(record, ApprovalRecord):
            raise TypeError(f"record 必须是 ApprovalRecord：{type(record).__name__}")
        with self._session() as session:
            session.add(
                AgentApprovalORM(
                    approval_id=record.approval_id,
                    run_id=record.run_id,
                    requested_action=record.requested_action,
                    risk_level=record.risk_level,
                    requested_by=record.requested_by,
                    decided_by=record.decided_by,
                    decision=record.decision,
                    reason=record.reason,
                    requested_at=record.requested_at,
                    decided_at=record.decided_at,
                )
            )
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise TaskConflictError(f"approval_id 重复：{record.approval_id}") from exc
        return record

    def get(self, approval_id: str) -> ApprovalRecord | None:
        if not isinstance(approval_id, str):
            raise TypeError(f"approval_id 必须是 str：{type(approval_id).__name__}")
        with self._session() as session:
            row = session.execute(
                select(AgentApprovalORM).where(AgentApprovalORM.approval_id == approval_id)
            ).scalar_one_or_none()
            return _row_to_approval(row) if row is not None else None

    def list_pending(self, run_id: str | None = None) -> list[ApprovalRecord]:
        if run_id is not None and not isinstance(run_id, str):
            raise TypeError(f"run_id 必须是 str 或 None：{type(run_id).__name__}")
        with self._session() as session:
            stmt = (
                select(AgentApprovalORM)
                .where(AgentApprovalORM.decision.is_(None))
                .order_by(AgentApprovalORM.requested_at)
            )
            if run_id is not None:
                stmt = stmt.where(AgentApprovalORM.run_id == run_id)
            rows = session.execute(stmt).scalars().all()
            return [_row_to_approval(row) for row in rows]

    def list_all(self) -> list[ApprovalRecord]:
        with self._session() as session:
            rows = session.execute(
                select(AgentApprovalORM).order_by(AgentApprovalORM.requested_at)
            ).scalars().all()
            return [_row_to_approval(row) for row in rows]

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
        with self._session() as session:
            row = session.execute(
                select(AgentApprovalORM).where(AgentApprovalORM.approval_id == approval_id)
            ).scalar_one_or_none()
            if row is None:
                session.rollback()
                raise AgentNotFoundError(f"审批不存在：{approval_id}")
            if row.decision is not None and row.decision != "cancelled":
                session.rollback()
                raise TaskConflictError(f"审批 {approval_id} 已决策：{row.decision}")
            if decision not in ("approved", "rejected", "cancelled"):
                session.rollback()
                raise AgentNotFoundError(f"非法审批决策：{decision}")
            row.decision = decision
            row.decided_by = decided_by
            row.reason = reason
            row.decided_at = datetime.now(timezone.utc)
            session.commit()
            return _row_to_approval(row)

    def count(self) -> int:
        with self._session() as session:
            return int(
                session.execute(select(func.count()).select_from(AgentApprovalORM)).scalar_one()
            )
