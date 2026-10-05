"""确定性 Agent Runtime 内核（WP-01 首版，纯内存，零外部依赖）。

★ 设计目标（手册第 4 节 WP-01）：
    不依赖数据库 / Redis / MQTT / 外网 / 大模型即可运行测试。

核心入口（手册 3.6）：
    AgentRuntime.run(request)    -> AgentRunResult
    AgentRuntime.cancel(run_id, actor, reason) -> AgentRunResult
    AgentRuntime.resume(run_id)  -> AgentRunResult
    AgentRuntime.status()        -> RuntimeStatus

可注入依赖（手册 3.6）：tool_registry / policy_guard / memory_store /
run_repository / approval_repository / clock / id_factory / runtime_config。

状态机（手册 3.1）：
    created -> planning -> waiting_policy -> waiting_approval
             -> executing -> observing -> verifying
             -> succeeded | failed | cancelled | expired（终态不可再迁移）

确定性规则模式：
    runtime_config.model_available=False（默认）时无模型照常工作；
    计划由 rule_plan 模板生成，期望由 Expectation 规则判定，全部可复现。

★ 冻结语义：规划步骤的轨迹表示（总控裁定，WP-03/WP-05 必须沿用）：
    - 一次规划阶段（plan phase）恰好产出**一条 plan 步骤**，decision_summary
      为摘要式：「计划意图 + 计划动作数 + 关键要点」（手册 3.7 解密预算：
      只保存决策摘要，动作级细节不进轨迹）。
    - 重规划阶段产出**一条 replan 步骤**（重规划原因摘要）+ 随附**一条新的
      plan 步骤**（同样是摘要式）。
    - 任何 plan/replan 步骤都只追加、不覆盖历史步骤（手册 3.2）。

解密预算（手册 3.7）：
    轨迹只写 decision_summary（决策摘要）与 input_hash / output_hash，
    记忆只写摘要与业务理由，绝不保存模型私有思维链。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from app.services.agents.errors import (
    ERROR_CODES,
    AgentError,
    AgentNotFoundError,
    ErrorCode,
    InternalAgentError,
)
from app.services.agents.memory import InMemoryMemoryStore, MemoryEntry, MemoryStore
from app.services.agents.lessons import LessonStore, summarize_hits
from app.services.agents.model import (
    TERMINAL_STATUSES,
    AgentRun,
    AgentRunRequest,
    AgentRunResult,
    AgentStatus,
    AgentStep,
    AgentStepType,
    LEGAL_TRANSITIONS,
    RuntimeConfig,
    RuntimeStatus,
    RuleStep,
)
from app.services.agents.model_adapter import ModelAdapterPlanner, summarize_tools
from app.services.agents.planner import RulePlanner
from app.services.agents.policy import PolicyGuard, RulePolicyGuard
from app.services.agents.repositories import (
    ApprovalRecord,
    ApprovalRepository,
    InMemoryApprovalRepository,
    InMemoryRunRepository,
    RunRepository,
)
from app.services.agents.schema import sha256_hex
from app.services.agents.tools import ToolDefinition, ToolExecutor, ToolRegistry, ToolResult

logger = logging.getLogger("oceanus.agents")

# ----------------------------------------------------------------------
# 时钟与 ID 工厂（可注入，测试可确定复现）
# ----------------------------------------------------------------------


class Clock(Protocol):
    """可注入时钟：now() 返回业务时间，now_ms() 返回单调毫秒。"""

    def now(self) -> datetime: ...

    def now_ms(self) -> int: ...


class SystemClock:
    """真实时钟（默认）。now() 返回 aware UTC，与 PG TIMESTAMPTZ 语义一致。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_ms(self) -> int:
        return int(time.time() * 1000)


class FakeClock:
    """假时钟：测试注入，advance() 推进，now()/now_ms() 同源一致。

    now() 返回 aware UTC（与 SystemClock 对齐）：naive 时间戳写入 PG
    TIMESTAMPTZ 会被按会话时区解释，读回后偏移导致审批/过期误判超时。
    """

    def __init__(self, start: datetime | None = None, start_ms: int = 0) -> None:
        start = start or datetime(2026, 1, 1, 0, 0, 0)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self._start = start
        self._ms = start_ms

    def now(self) -> datetime:
        return self._start + timedelta(milliseconds=self._ms)

    def now_ms(self) -> int:
        return self._ms

    def advance(self, ms: int) -> None:
        self._ms += ms


def _as_aware(dt: datetime | None) -> datetime | None:
    """统一为带时区时间用于比较：naive 视为 UTC；None 原样返回。

    时钟（FakeClock/SystemClock）返回 aware UTC；数据库仓储（PG TIMESTAMPTZ）
    读回的记录同样 aware。_as_aware 是对第三方/遗留 naive 值的防御：直接相减
    naive 与 aware 会抛 TypeError（SQLite 不暴露此差异），naive 按 UTC 解释与
    PG 存储语义一致。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class IdFactory(Protocol):
    """可注入 ID 工厂：new(prefix) 生成带冻结前缀的 ID。

    协议方法体做参数校验后抛 NotImplementedError：既保证形参被真实
    使用（静态守卫），又保持纯协议语义。
    """

    def new(self, prefix: str) -> str:
        if not isinstance(prefix, str) or not prefix:
            raise TypeError(f"prefix 必须是非空 str：{prefix!r}")
        raise NotImplementedError("IdFactory.new 未实现")


class SequenceIdFactory:
    """序列 ID 工厂：确定性可复现（测试用，run_0001、stp_0001……）。"""

    def __init__(self, start: int = 1, width: int = 4) -> None:
        self._n = start
        self._width = width

    def new(self, prefix: str) -> str:
        value = self._n
        self._n += 1
        return f"{prefix}{value:0{self._width}d}"


class UuidIdFactory:
    """UUID 截断 ID 工厂（默认，保证跨进程唯一）。"""

    def new(self, prefix: str) -> str:
        return f"{prefix}{uuid.uuid4().hex[:12]}"


# ----------------------------------------------------------------------
# Agent Runtime
# ----------------------------------------------------------------------


class AgentRuntime:
    """确定性 Agent 运行内核。

    用法：
        runtime = AgentRuntime()                      # 全部默认依赖
        result = runtime.run(AgentRunRequest(...))    # 同步返回
        runtime.resume(run_id) / runtime.cancel(...)
    """

    def __init__(
        self,
        *,
        tool_registry: ToolRegistry | None = None,
        policy_guard: PolicyGuard | None = None,
        memory_store: MemoryStore | None = None,
        run_repository: RunRepository | None = None,
        approval_repository: ApprovalRepository | None = None,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        runtime_config: RuntimeConfig | None = None,
        model_adapter: ModelAdapterPlanner | None = None,
        lesson_store: LessonStore | None = None,
    ) -> None:
        self.config = runtime_config or RuntimeConfig()
        self.clock: Clock = clock or SystemClock()
        self.id_factory: IdFactory = id_factory or UuidIdFactory()
        self.registry = tool_registry or ToolRegistry()
        self.policy_guard: PolicyGuard = policy_guard or RulePolicyGuard(self.config)
        self.memory: MemoryStore = memory_store or InMemoryMemoryStore(
            id_factory=self.id_factory, clock=self.clock
        )
        # 跨 run 经验：与 run 级 working 记忆共用同一个 MemoryStore，
        # 于是「经验」与「记忆」天然遵循同一套脱敏与留痕纪律。
        self.lessons: LessonStore = lesson_store or LessonStore(
            self.memory,
            enabled=self.config.lessons_enabled,
            id_factory=self.id_factory,
            clock=self.clock,
        )
        self.runs: RunRepository = run_repository or InMemoryRunRepository()
        self.approvals: ApprovalRepository = approval_repository or InMemoryApprovalRepository()
        self.executor = ToolExecutor(self.registry, self.policy_guard, self.clock)
        self._planner = RulePlanner(self.config)
        self.model_adapter = model_adapter
        self._boot_ms = self.clock.now_ms()

    # ------------------------------------------------------------------
    # 公共入口（手册 3.6）
    # ------------------------------------------------------------------

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        self._sweep_expired()
        # 重复触发幂等：同一幂等键复用已有 run，不新建
        if request.idempotency_key:
            existing = self.runs.find_by_idempotency_key(request.idempotency_key)
            if existing is not None:
                logger.info("[Agent] 幂等触发复用 run %s（key=%s）", existing.run_id, request.idempotency_key)
                return self._result(existing, idempotent_replay=True)

        now = self.clock.now()
        run = AgentRun(
            run_id=self.id_factory.new("run_"),
            trigger_type=request.trigger_type,
            objective=request.objective,
            status=AgentStatus.CREATED.value,
            policy_version=None,
            started_at=now,
            finished_at=None,
            termination_reason=None,
            trace_id=request.trace_id or uuid.uuid4().hex,
            created_at=now,
            updated_at=now,
            request=request,
            runtime_state={"stage": "created"},
        )
        self.runs.create(run)
        return self._advance(run)

    def resume(self, run_id: str) -> AgentRunResult:
        run = self.runs.get(run_id)
        if run is None:
            raise AgentNotFoundError(f"run 不存在：{run_id}")
        if run.status in TERMINAL_STATUSES:
            return self._result(run)  # 终态不得再次迁移
        self._sweep_expired()
        refreshed = self.runs.get(run_id)
        if refreshed.status in TERMINAL_STATUSES:
            return self._result(refreshed)
        return self._advance(refreshed)

    def cancel(self, run_id: str, actor: str, reason: str) -> AgentRunResult:
        run = self.runs.get(run_id)
        if run is None:
            raise AgentNotFoundError(f"run 不存在：{run_id}")
        if run.status in TERMINAL_STATUSES:
            return self._result(run)  # 终态不可再迁移，cancel 为幂等 no-op

        # 挂起中的审批随取消关闭
        for approval_id in run.runtime_state.get("approvals") or []:
            record = self.approvals.get(approval_id)
            if record is not None and record.decision is None:
                self.approvals.decide(approval_id, actor, "cancelled", reason or "run 被取消")

        self._add_terminal_step(
            run,
            status=AgentStatus.CANCELLED.value,
            decision_summary=f"run 被 {actor} 取消：{reason}",
            error_code=None,
        )
        run.status = AgentStatus.CANCELLED.value
        run.termination_reason = AgentStatus.CANCELLED.value
        run.finished_at = self.clock.now()
        run.updated_at = self.clock.now()
        self.runs.save(run)
        return self._result(run)

    def status(self) -> RuntimeStatus:
        runs = self.runs.list()
        active = [r for r in runs if r.status not in TERMINAL_STATUSES]
        if self.model_adapter is None:
            model_available = self.config.model_available
        else:
            model_available = bool(
                self.model_adapter.enabled and self.model_adapter.client is not None
            )
        return RuntimeStatus(
            state="running" if active else "idle",
            model_available=model_available,
            rule_mode=not model_available,
            policy_version=self.config.policy_version,
            active_runs=len(active),
            total_runs=len(runs),
            pending_approvals=len(self.approvals.list_pending()),
            tools_registered=len(self.registry),
            uptime_ms=self.clock.now_ms() - self._boot_ms,
        )

    # ------------------------------------------------------------------
    # 状态机推进（run 与 resume 共用）
    # ------------------------------------------------------------------

    def _advance(self, run: AgentRun) -> AgentRunResult:
        while True:
            stage = run.runtime_state.get("stage", "created")
            if stage == "created":
                result = self._stage_planning(run)
            elif stage in ("planning", "waiting_policy"):
                # 阶段名与状态对齐：planning 阶段产出计划并置 waiting_policy；
                # waiting_policy 阶段执行策略守卫（replan 也会回到 planning）
                result = self._stage_policy(run)
            elif stage == "waiting_approval":
                outcome = self._resolve_approvals(run)
                if outcome == "pending":
                    return self._result(run)  # 审批未决，保持挂起
                if outcome == "rejected":
                    return self._fail(run, ErrorCode.APPROVAL_REJECTED, "人工审批拒绝", verify=False)
                if outcome == "timeout":
                    return self._fail(run, ErrorCode.APPROVAL_TIMEOUT, "人工审批超时", verify=False)
                run.runtime_state["stage"] = "executing"
                result = None
            elif stage == "executing":
                result = self._stage_execute(run)
            elif stage == "verifying":
                result = self._succeed(run)
            else:
                result = self._fail(run, ErrorCode.INTERNAL_ERROR, f"未知阶段：{stage}", verify=False)
            if isinstance(result, AgentRunResult):
                return result
            # result is None -> 继续推进

    def _stage_planning(self, run: AgentRun) -> None:
        self._transition(run, AgentStatus.PLANNING)
        try:
            plan, source, error_code = self._build_plan(run)
        except AgentError as exc:
            return self._fail(run, ErrorCode.INTERNAL_ERROR, f"规划失败：{exc}", verify=False)
        if not plan:
            return self._fail(run, ErrorCode.INTERNAL_ERROR, "规划结果为空", verify=False)
        run.runtime_state["plan"] = plan
        run.runtime_state["plan_idx"] = 0
        run.runtime_state["bindings"] = dict(run.request.params or {})
        run.runtime_state["replan_count"] = 0
        # 每个规划周期落一条 plan 步骤（决策摘要枚举全部动作；重规划时
        # 新增 replan + 新的 plan 步骤，不覆盖历史）
        plan_summary = self._plan_summary(plan, source=source, error_code=error_code)
        role_summary = self._describe_plan_roles(plan)
        if role_summary:
            plan_summary = f"{plan_summary}；{role_summary}"
        lesson_note = self._consult_lessons(run)
        if lesson_note:
            plan_summary = f"{plan_summary}；{lesson_note}"
        if not self._add_step(
            run,
            step_type=AgentStepType.PLAN,
            decision_summary=plan_summary,
        ):
            return self._fail(run, ErrorCode.MAX_STEPS_EXCEEDED, "步骤预算耗尽（计划）", verify=False)
        run.runtime_state["stage"] = "waiting_policy"
        return None

    def _consult_lessons(self, run: AgentRun) -> str:
        """规划阶段检索跨 run 经验，命中则返回写进计划摘要的那句话。

        ★ 为什么不做「经验直接改计划」：那会让同一条经验在不同版本间
          产生难以复现的行为漂移，而且评审核对时无法解释"这一步为什么
          和上次不一样"。这里只把经验写进**决策摘要**（给人和审计看），
          计划本身仍由确定性规则生成 —— 经验影响的是"解释"与"优先级提示"，
          不是隐藏地改写控制流。
        """
        main_class = str(run.request.params.get("main_class") or "")
        if not main_class:
            return ""
        hits = self.lessons.match(
            main_class=main_class,
            context=dict(run.request.lessons_context or {}),
        )
        if not hits:
            return ""
        self.lessons.note_hit(hits, run_id=run.run_id)
        run.runtime_state["lesson_hits"] = [lesson.lesson_id for lesson in hits]
        return summarize_hits(hits)

    def _build_plan(self, run: AgentRun) -> tuple[list[RuleStep], str | None, str | None]:
        """生成一次计划；适配器启用时由适配器负责模型优先与规则兜底。"""
        if self.model_adapter is None:
            return self._planner.build(run.request), None, None
        proposal = self.model_adapter.plan(
            task=run.request,
            business_context=dict(run.runtime_state.get("business_context") or {}),
            tool_summary=summarize_tools(self.registry),
            role=run.request.role,
        )
        return list(proposal.steps), proposal.source, proposal.error_code

    @staticmethod
    def _plan_summary(
        plan: list[RuleStep],
        *,
        source: str | None = None,
        error_code: str | None = None,
    ) -> str:
        actions = "; ".join(f"{s.tool}（{s.summary or '无摘要'}）" for s in plan)
        if source is None:
            return f"规则计划 {len(plan)} 步：{actions}"
        fallback = f" code={error_code}" if error_code else ""
        return f"规划 source={source}{fallback}，{len(plan)} 步：{actions}"

    @staticmethod
    def _describe_plan_roles(plan: list[RuleStep]) -> str:
        """多角色计划的人话摘要：按角色分组列出动作。

        单角色计划（全部 role 为 None）返回空串，调用方保持原有摘要文案不变 ——
        评测与既有断言对 `_plan_summary` 的输出是敏感的，不能顺手改掉。
        """
        if not any(step.role for step in plan):
            return ""
        groups: list[tuple[str, list[RuleStep]]] = []
        for step in plan:
            role = step.role or "编排"
            if groups and groups[-1][0] == role:
                groups[-1][1].append(step)
            else:
                groups.append((role, [step]))
        parts = [
            "【{role}】{actions}".format(
                role=role,
                actions="; ".join(
                    f"{s.tool}（{s.summary or '无摘要'}）" for s in steps
                ),
            )
            for role, steps in groups
        ]
        return "角色分工：" + " → ".join(parts)

    def _stage_policy(self, run: AgentRun) -> None:
        self._transition(run, AgentStatus.WAITING_POLICY)
        plan: list[RuleStep] = run.runtime_state["plan"]
        decision = self.policy_guard.check_plan(plan, run.request, self.registry)
        run.policy_version = decision.policy_version or self.config.policy_version
        if not self._add_step(
            run,
            step_type=AgentStepType.POLICY,
            status="ok" if decision.allow else "failed",
            decision_summary=f"策略守卫（{run.policy_version}）：{decision.reason}",
            error_code=None if decision.allow else ErrorCode.POLICY_DENIED,
        ):
            return self._fail(run, ErrorCode.MAX_STEPS_EXCEEDED, "步骤预算耗尽（策略）", verify=False)
        if not decision.allow:
            return self._fail(run, ErrorCode.POLICY_DENIED, decision.reason, verify=False)

        # 审批预检：对需要审批的计划步骤一次性创建审批单并挂起
        approval_ids: list[str] = []
        for step in plan:
            tool = self.registry.get(step.tool)
            if tool is None:
                continue  # 执行阶段会以 tool_failed 失败
            tool_decision = self.policy_guard.check_tool(tool, run.request.role, {})
            if tool_decision.requires_approval:
                record = self.approvals.create(
                    ApprovalRecord(
                        approval_id=self.id_factory.new("apr_"),
                        run_id=run.run_id,
                        requested_action=f"{step.tool}：{step.summary}",
                        risk_level=(
                            tool_decision.risk_level.value
                            if tool_decision.risk_level is not None
                            else str(tool.risk_level)
                        ),
                        requested_by=run.request.actor,
                        requested_at=self.clock.now(),
                    )
                )
                if not self._add_step(
                    run,
                    step_type=AgentStepType.APPROVAL_REQUEST,
                    status="pending",
                    decision_summary=f"请求人工审批：{record.requested_action}（风险 {record.risk_level}）",
                ):
                    return self._fail(run, ErrorCode.MAX_STEPS_EXCEEDED, "步骤预算耗尽（审批）", verify=False)
                approval_ids.append(record.approval_id)
        if approval_ids:
            run.runtime_state["approvals"] = approval_ids
            run.runtime_state["stage"] = "waiting_approval"
            self._transition(run, AgentStatus.WAITING_APPROVAL)
            return None
        run.runtime_state["stage"] = "executing"
        self._transition(run, AgentStatus.EXECUTING)
        return None

    def _stage_execute(self, run: AgentRun) -> AgentRunResult | None:
        plan: list[RuleStep] = run.runtime_state["plan"]
        bindings: dict[str, Any] = run.runtime_state.setdefault("bindings", {})
        idx = int(run.runtime_state.get("plan_idx", 0))
        role = run.request.role

        while idx < len(plan):
            step = plan[idx]
            tool = self.registry.get(step.tool)
            if tool is None:
                return self._fail(run, ErrorCode.TOOL_FAILED, f"工具未注册：{step.tool}")

            self._transition(run, AgentStatus.EXECUTING)
            try:
                input_ = self._planner.bind(step, bindings)
            except AgentError as exc:
                return self._fail(run, exc.code or ErrorCode.INVALID_TOOL_INPUT, str(exc))

            # 执行（含重试上限：每次尝试都落一条 tool_call 步骤）
            attempt = 0
            final: ToolResult | None = None
            while True:
                attempt += 1
                final = self._execute_once_step(run, tool, step, input_, role)
                if final.ok or attempt > self.config.retry_limit:
                    break
                if final.error_code not in (ErrorCode.TOOL_TIMEOUT, ErrorCode.TOOL_FAILED):
                    break  # 策略/输入/输出错误不可重试

            self._transition(run, AgentStatus.OBSERVING)
            summary = self._observe_summary(step, final)
            if not self._add_step(
                run,
                step_type=AgentStepType.OBSERVATION,
                decision_summary=summary,
                role=step.role,
            ):
                return self._fail(run, ErrorCode.MAX_STEPS_EXCEEDED, "步骤预算耗尽（观察）")
            self._save_memory(run, source_type="tool_call", summary=summary)

            if not final.ok:
                code = final.error_code or ErrorCode.TOOL_FAILED
                if step.on_error == "replan":
                    if self._replan(run, code):
                        plan = run.runtime_state["plan"]
                        idx = int(run.runtime_state.get("plan_idx", 0))
                        continue
                    return self._fail(run, ErrorCode.MAX_STEPS_EXCEEDED, f"重规划预算耗尽（{code}）")
                return self._fail(run, code, final.error_message or f"步骤 {step.tool} 执行失败")

            if step.expect is not None:
                verdict, expect_code = self._check_expectation(step.expect, final.data)
                if verdict == "replan":
                    if self._replan(run, expect_code):
                        plan = run.runtime_state["plan"]
                        idx = int(run.runtime_state.get("plan_idx", 0))
                        continue
                    return self._fail(run, ErrorCode.MAX_STEPS_EXCEEDED, f"重规划预算耗尽（{expect_code}）")
                if verdict == "terminate":
                    return self._fail(
                        run, expect_code, f"步骤 {step.tool} 未满足期望（{step.expect.key}）"
                    )

            if isinstance(final.data, dict):
                bindings.update(final.data)
            idx += 1
            run.runtime_state["plan_idx"] = idx

        run.runtime_state["stage"] = "verifying"
        return None

    # ------------------------------------------------------------------
    # 步骤记录
    # ------------------------------------------------------------------

    def _add_step(
        self,
        run: AgentRun,
        *,
        step_type: AgentStepType,
        decision_summary: str,
        status: str = "ok",
        tool_name: str | None = None,
        tool_version: str | None = None,
        input_hash: str | None = None,
        output_hash: str | None = None,
        latency_ms: int = 0,
        error_code: str | None = None,
        role: str | None = None,
    ) -> AgentStep | None:
        step_no = len(self.runs.steps(run.run_id)) + 1
        if step_no > self.config.max_steps:
            return None  # 步骤预算耗尽 → 安全终止
        step = AgentStep(
            step_id=self.id_factory.new("stp_"),
            run_id=run.run_id,
            step_no=step_no,
            step_type=step_type.value,
            decision_summary=decision_summary,
            tool_name=tool_name,
            tool_version=tool_version,
            input_hash=input_hash,
            output_hash=output_hash,
            status=status,
            latency_ms=latency_ms,
            error_code=error_code,
            created_at=self.clock.now(),
        )
        self.runs.add_step(step)
        self._record_step_role(run, step_no, role)
        return step

    @staticmethod
    def _record_step_role(run: AgentRun, step_no: int, role: str | None) -> None:
        """把「这一步是哪个角色做的」记进 run 状态快照。

        ★ 刻意不写进 t_agent_step：手册 3.7 的步骤表是冻结契约，角色归属
          是内核的运行期元数据。存在 runtime_state 里，既能跟随
          `persistent_repository` 的状态快照一起持久化（重启后只读回放
          仍有角色），又不需要任何 DDL。
        """
        if not role:
            return
        roles = run.runtime_state.setdefault("step_roles", {})
        if isinstance(roles, dict):
            roles[str(step_no)] = role

    def _add_terminal_step(
        self,
        run: AgentRun,
        *,
        status: str,
        decision_summary: str,
        error_code: str | None,
    ) -> None:
        """终态步骤必须记录，不受 max_steps 限制。"""
        step = AgentStep(
            step_id=self.id_factory.new("stp_"),
            run_id=run.run_id,
            step_no=len(self.runs.steps(run.run_id)) + 1,
            step_type=AgentStepType.TERMINAL.value,
            decision_summary=decision_summary,
            status=status,
            error_code=error_code if error_code in ERROR_CODES else None,
            created_at=self.clock.now(),
        )
        self.runs.add_step(step)

    def _execute_once_step(
        self,
        run: AgentRun,
        tool: ToolDefinition,
        step: RuleStep,
        input_: dict[str, Any],
        role: str,
    ) -> ToolResult:
        result = self.executor.execute_once(run.run_id, role, tool.name, input_)
        in_hash = sha256_hex(input_)
        out_hash = result.output_hash
        if out_hash is None:
            out_hash = (
                sha256_hex({"error": result.error_code, "message": result.error_message})
                if not result.ok
                else None
            )
        # ★ 期望校验并入工具步骤状态：工具本身执行成功但业务期望未达成时，
        #   该条 tool_call 步骤必须落 failed（error_code=期望错误码），
        #   审计不允许静默丢弃（观察→验证→重规划的失败必须可追溯）。
        expect_ok = True
        expect_code: str | None = None
        if result.ok and step.expect is not None:
            verdict, expect_code = self._check_expectation(step.expect, result.data)
            expect_ok = verdict == "ok"
        step_ok = result.ok and expect_ok
        step_error_code = (
            result.error_code if not result.ok else (expect_code if not expect_ok else None)
        )
        self._add_step(
            run,
            step_type=AgentStepType.TOOL_CALL,
            status="ok" if step_ok else "failed",
            decision_summary=(
                f"调用 {tool.name} v{tool.version}：{step.summary or step.tool}"
                + ("（幂等重放）" if result.replayed else "")
                + ("" if step_ok else f"（期望未达成：{expect_code}）")
            ),
            tool_name=tool.name,
            tool_version=tool.version,
            input_hash=in_hash,
            output_hash=out_hash,
            latency_ms=result.latency_ms,
            error_code=step_error_code,
            role=step.role,
        )
        return result

    # ------------------------------------------------------------------
    # 验证 / 重规划 / 终止
    # ------------------------------------------------------------------

    def _check_expectation(self, expect: Any, data: dict[str, Any] | None) -> tuple[str, str]:
        """返回 (verdict, error_code)；verdict ∈ {"ok","terminate","replan"}。"""
        data = data or {}
        verdict = "terminate" if expect.on_violation == "terminate" else "replan"
        if expect.key not in data:
            return verdict, expect.error_code
        value = data[expect.key]
        if expect.min_items is not None and isinstance(value, (list, tuple, dict, str)):
            if len(value) < expect.min_items:
                return verdict, expect.error_code
        if expect.contains and value not in expect.contains:
            return verdict, expect.error_code
        return "ok", expect.error_code

    def _replan(self, run: AgentRun, reason_code: str) -> bool:
        """观察→验证→重规划：追加 verification(failed) + replan + 新 plan 步骤。

        ★ 只追加、不覆盖历史步骤（手册 3.2）；预算不足返回 False，
          由调用方安全终止（max_steps_exceeded 或原错误码）。
        """
        replan_count = int(run.runtime_state.get("replan_count", 0))
        if replan_count >= self.config.max_replans:
            return False
        if run.status != AgentStatus.VERIFYING.value:
            self._transition(run, AgentStatus.VERIFYING)
        if not self._add_step(
            run,
            step_type=AgentStepType.VERIFICATION,
            status="failed",
            decision_summary=f"重规划前验证失败：{reason_code}",
            error_code=reason_code,
        ):
            return False
        self._transition(run, AgentStatus.PLANNING)
        if not self._add_step(
            run,
            step_type=AgentStepType.REPLAN,
            decision_summary=f"第 {replan_count + 1} 次重规划（原因 {reason_code}）",
        ):
            return False
        try:
            new_plan, source, error_code = self._build_plan(run)
        except AgentError:
            return False
        if not new_plan:
            return False
        replan_summary = self._plan_summary(new_plan, source=source, error_code=error_code)
        replan_roles = self._describe_plan_roles(new_plan)
        if replan_roles:
            replan_summary = f"{replan_summary}；{replan_roles}"
        if not self._add_step(
            run,
            step_type=AgentStepType.PLAN,
            decision_summary=replan_summary,
        ):
            return False
        run.runtime_state["plan"] = new_plan
        run.runtime_state["plan_idx"] = 0
        run.runtime_state["replan_count"] = replan_count + 1
        return True

    def _terminate(
        self,
        run: AgentRun,
        status: AgentStatus,
        reason: str,
        message: str,
        *,
        verify: bool,
    ) -> AgentRunResult:
        if verify:
            if run.status != AgentStatus.VERIFYING.value:
                self._transition(run, AgentStatus.VERIFYING)
            ok = status == AgentStatus.SUCCEEDED
            self._add_step(
                run,
                step_type=AgentStepType.VERIFICATION,
                status="ok" if ok else "failed",
                decision_summary="所有计划步骤验证通过" if ok else f"验证失败：{message}",
                error_code=None if ok else (reason if reason in ERROR_CODES else None),
            )
        self._add_terminal_step(
            run,
            status=status.value,
            decision_summary=message,
            error_code=reason if reason in ERROR_CODES else None,
        )
        run.status = status.value
        run.termination_reason = reason
        run.finished_at = self.clock.now()
        run.updated_at = self.clock.now()
        self._harvest_lessons(run, outcome=self._outcome_of(status), reason=reason)
        self.runs.save(run)
        return self._result(run)

    @staticmethod
    def _outcome_of(status: AgentStatus) -> str:
        """把终态映射成复盘口径：只有 succeeded / failed 值得复盘。"""
        if status is AgentStatus.SUCCEEDED:
            return "succeeded"
        if status is AgentStatus.FAILED:
            return "failed"
        return "other"

    def _harvest_lessons(self, run: AgentRun, *, outcome: str, reason: str) -> None:
        """run 到终态时做一次确定性复盘，把可复用的结论沉淀成经验。

        ★ 只在 succeeded / failed 上做：取消与超时是"人的决定"或"环境问题"，
          从中提炼不出可复用规律，硬编一条只会污染经验库。
        ★ 复盘用的 bindings 是执行期真实产生的绑定变量（工具输出），
          不是再算一遍 —— 经验和轨迹必然一致。
        """
        if outcome not in ("succeeded", "failed"):
            return
        main_class = str(run.request.params.get("main_class") or "")
        if not main_class:
            return
        bindings = run.runtime_state.get("bindings")
        harvested = self.lessons.harvest(
            run_id=run.run_id,
            main_class=main_class,
            outcome=outcome,
            termination_reason=reason,
            bindings=bindings if isinstance(bindings, dict) else {},
            replan_count=int(run.runtime_state.get("replan_count", 0) or 0),
        )
        if harvested:
            run.runtime_state["lessons_harvested"] = [item.lesson_id for item in harvested]

    def _succeed(self, run: AgentRun) -> AgentRunResult:
        return self._terminate(
            run,
            AgentStatus.SUCCEEDED,
            AgentStatus.SUCCEEDED.value,
            "run 成功，正常终止",
            verify=True,
        )

    def _fail(
        self,
        run: AgentRun,
        code: str,
        message: str,
        *,
        verify: bool = True,
    ) -> AgentRunResult:
        return self._terminate(run, AgentStatus.FAILED, code, message, verify=verify)

    def _expire(self, run: AgentRun) -> AgentRunResult:
        return self._terminate(
            run,
            AgentStatus.EXPIRED,
            ErrorCode.RUN_EXPIRED,
            "运行超过总时长上限，已过期",
            verify=False,
        )

    # ------------------------------------------------------------------
    # 审批 / 过期 / 记忆 / 结果
    # ------------------------------------------------------------------

    def _resolve_approvals(self, run: AgentRun) -> str:
        """返回 pending / approved / rejected / timeout。"""
        approval_ids = run.runtime_state.get("approvals") or []
        any_pending = False
        for approval_id in approval_ids:
            record = self.approvals.get(approval_id)
            if record is None:
                continue
            if record.decision is None:
                if record.requested_at is None:
                    # 无请求时间（如手工构造的待决审批）：无法判超时，视为 pending
                    any_pending = True
                    continue
                elapsed_ms = (
                    _as_aware(self.clock.now()) - _as_aware(record.requested_at)
                ).total_seconds() * 1000
                if elapsed_ms > self.config.approval_timeout_ms:
                    return "timeout"
                any_pending = True
            elif record.decision != "approved":
                return "rejected"
        if any_pending:
            return "pending"
        return "approved"

    def _sweep_expired(self) -> None:
        """扫描超时未完成的 run；waiting_approval 的审批超时在 resume 判定。"""
        now = self.clock.now()
        for run in self.runs.list():
            if run.status in TERMINAL_STATUSES:
                continue
            if run.status == AgentStatus.WAITING_APPROVAL.value:
                continue
            if run.started_at is None:
                continue
            elapsed_ms = (
                _as_aware(now) - _as_aware(run.started_at)
            ).total_seconds() * 1000
            if elapsed_ms > self.config.run_ttl_ms:
                logger.warning("[Agent] run %s 运行超时（%.0fms > %dms）", run.run_id, elapsed_ms, self.config.run_ttl_ms)
                self._expire(run)

    def _save_memory(self, run: AgentRun, *, source_type: str, summary: str) -> None:
        now = self.clock.now()
        entry = MemoryEntry(
            memory_id=self.id_factory.new("mem_"),
            memory_type="working",
            scope_type="run",
            scope_id=run.run_id,
            content={"summary": summary, "business_reason": None},
            confidence=1.0,
            source_type=source_type,
            source_id=run.run_id,
            valid_from=now,
            valid_to=None,
            created_at=now,
        )
        try:
            self.memory.save(entry)
        except AgentError as exc:
            logger.warning("[Agent] 记忆写入失败（解密预算）：%s", exc)

    def _observe_summary(self, step: RuleStep, result: ToolResult) -> str:
        if result.ok:
            return f"观察：{step.tool} 返回 {self._summarize(result.data)}"
        return (
            f"观察：{step.tool} 失败（{result.error_code or 'tool_failed'}）"
            f"{result.error_message or ''}"
        )

    @staticmethod
    def _summarize(data: Any, depth: int = 0) -> str:
        """输出摘要（解密预算：只留摘要，不留原始载荷）。"""
        if data is None:
            return "null"
        if isinstance(data, dict):
            if depth >= 2:
                return f"object[{len(data)}]"
            parts = []
            for key, value in list(data.items())[:5]:
                parts.append(f"{key}={AgentRuntime._summarize(value, depth + 1)}")
            tail = "…" if len(data) > 5 else ""
            return "{" + ", ".join(parts) + tail + "}"
        if isinstance(data, (list, tuple)):
            return f"list[{len(data)}]"
        text = str(data)
        return text if len(text) <= 40 else text[:37] + "..."

    def _result(self, run: AgentRun, *, idempotent_replay: bool = False) -> AgentRunResult:
        pending = [
            approval_id
            for approval_id in run.runtime_state.get("approvals") or []
            if (record := self.approvals.get(approval_id)) is not None and record.decision is None
        ]
        error_code = (
            run.termination_reason
            if run.status in (AgentStatus.FAILED.value, AgentStatus.EXPIRED.value)
            else None
        )
        return AgentRunResult(
            run_id=run.run_id,
            status=run.status,
            termination_reason=run.termination_reason,
            error_code=error_code,
            steps=self.runs.steps(run.run_id),
            trace_id=run.trace_id,
            objective=run.objective,
            trigger_type=run.trigger_type,
            policy_version=run.policy_version,
            finished_at=run.finished_at,
            pending_approval_ids=pending,
            idempotent_replay=idempotent_replay,
        )

    def _transition(self, run: AgentRun, target: AgentStatus) -> None:
        current = run.status
        if current in TERMINAL_STATUSES:
            raise InternalAgentError(f"终态 {current} 不得再次迁移")
        if current == target.value:
            return  # 同状态重申为 no-op（执行循环中 executing/observing 反复进入）
        # verifying 是终止出口斜坡：允许从任意非终态进入（安全终止路径）
        if target.value == AgentStatus.VERIFYING.value:
            run.status = target.value
            run.updated_at = self.clock.now()
            return
        if target.value not in LEGAL_TRANSITIONS.get(current, set()):
            raise InternalAgentError(f"非法状态迁移：{current} → {target.value}")
        run.status = target.value
        run.updated_at = self.clock.now()
