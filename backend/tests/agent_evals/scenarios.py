"""WP-05/WP-12 固定场景集：确定性离线评测的输入、期望结果与判定函数。

定位（计划书 5.7「评测记忆」）：固定场景、期望轨迹和失败样本，永久版本化。
本模块只依赖 WP-01 内核（backend/app/services/agents/**，只读使用）与
WP-10/WP-11/WP-14 公开接口（只读使用，缺失时场景明确跳过），全部使用内存
仓储 / SQLite 内存 / 假时钟 / 假模型客户端 / 内存 device transport，不连
数据库 / Redis / MQTT / 外网 / 模型 / 设备。

纪律（总控 E0-E4）：所有场景 evidence_level = E1（内部实现 / 确定性仿真
证据），禁止写成 E3/E4。

场景清单（WP-05 13 个，ID 与语义冻结；WP-12 追加 10 个；第四波追加 4 个，只增不替换）：
    1. normal_dispatch_success     正常派单闭环（成功）
    2. no_robot_available          无机器人（失败 → no_robot_available）
    3. policy_denied               策略拒绝（角色越权）
    4. approval_rejected           审批拒绝（不发送设备指令）
    5. approval_timeout            审批超时
    6. approval_approved           审批通过后继续执行（补充）
    7. tool_timeout                工具超时（重试上限内安全失败）
    8. max_steps_exceeded          最大步数安全终止
    9. invalid_loop_replan         无效循环（重复 replan 不上进）
   10. replan_recovery_success     可恢复异常重规划成功（恢复成功率分子）
   11. rule_mode_without_model     模型不可用 → 规则模式继续工作
   12. idempotent_replay           幂等重放（run 级 + 工具级）
   13. invalid_tool_output         输出 Schema 错误（补充）
   —— WP-12 第三波（docs/agent-program-wave3.md 3.3）——
   14. persistent_restart_resume      持久化重启续跑（WP-10）
   15. concurrent_idempotent_trigger  并发幂等触发（WP-10）
   16. stale_state_conflict           旧 state_version 乐观锁冲突（WP-10）
   17. model_valid_plan               合法模型计划（WP-11）
   18. model_invalid_json_fallback    非法 JSON 回退（WP-11）
   19. model_timeout_fallback         超时回退（WP-11）
   20. model_sensitive_tool_denied    敏感工具越权拒绝（WP-11）
   21. trace_replay_integrity         轨迹回放完整性（WP-12 replay）
   22. multi_role_handoff             多角色审批交接（WP-12）
   23. device_command_fault_injection 设备命令与故障注入（WP-14）
   —— 第四波（多角色研判门禁 + 跨 run 经验闭环）——
   24. team_assessor_gate_blocks       研判门禁拦截（证据不足 → 不自动派单）
   25. team_assessor_pass_then_dispatch 研判通过 → 调度四步全执行
   26. lessons_retro_and_reuse         跨 run 经验闭环（终态复盘 → 下次规划引用）
   27. lessons_scoped_and_evict        经验按 (scope, kind) 键控 + 计数与置信度封顶
"""

from __future__ import annotations

import copy
import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# ---- 路径引导：被 scripts/ 与 pytest 两种方式导入 ----
_EVAL_DIR = Path(__file__).resolve().parent
_BACKEND = _EVAL_DIR.parent.parent
for _p in (_BACKEND, _EVAL_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from app.services.agents import (  # noqa: E402
    AgentRun,
    AgentRunRequest,
    AgentRuntime,
    ErrorCode,
    FakeClock,
    RiskLevel,
    RuntimeConfig,
    SequenceIdFactory,
    TaskConflictError,
    ToolDefinition,
    ToolRegistry,
    UuidIdFactory,
)

# 仓库根目录（edge.device_sim 等非 backend 包需要）—— 命名空间包，无 __init__.py
_ROOT = _EVAL_DIR.parents[2]
for _p in (_ROOT,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# ----------------------------------------------------------------------
# 固定场景规格（纯数据，参与配置哈希）
# ----------------------------------------------------------------------

SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "normal_dispatch_success",
        "name": "正常派单闭环（成功）",
        "description": "事件可信、机器人可用：自动计划、派单、观察、完成（计划书 5.12 场景 1）",
        "expected": {
            "status": "succeeded",
            "error_code": None,
            "shape": "plan → policy → 5×(tool_call+observation) → verification → terminal",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    {
        "id": "no_robot_available",
        "name": "无机器人（失败 → no_robot_available）",
        "description": "设备查询无候选：不误报成功，记录原因，不建单不下发（计划书 5.12 场景 2）",
        "expected": {
            "status": "failed",
            "error_code": "no_robot_available",
            "shape": "plan → policy → tool_call(event.get)+observation → tool_call(device.query_available)+observation → verification(failed) → terminal",
        },
        "must_not_execute": ["dispatch.plan", "task.create_or_merge", "mission.observe"],
        "evidence_level": "E1",
    },
    {
        "id": "policy_denied",
        "name": "策略拒绝（角色越权）",
        "description": "角色不在工具 allowed_roles 内：计划被策略守卫阻断，产生结构化原因（计划书 5.12 场景 5）",
        "expected": {
            "status": "failed",
            "error_code": "policy_denied",
            "shape": "plan → policy(failed) → terminal",
        },
        "must_not_execute": ["event.get", "device.query_available", "dispatch.plan", "task.create_or_merge", "mission.observe"],
        "evidence_level": "E1",
    },
    {
        "id": "approval_rejected",
        "name": "审批拒绝（不发送设备指令）",
        "description": "敏感设备指令需人工审批，拒绝后 run 终止且不执行任何工具（计划书 5.12 场景 6）",
        "expected": {
            "status": "failed",
            "error_code": "approval_rejected",
            "shape": "plan → policy → approval_request(pending) → terminal",
        },
        "must_not_execute": ["event.get", "mqtt.send_task"],
        "evidence_level": "E1",
    },
    {
        "id": "approval_timeout",
        "name": "审批超时",
        "description": "审批超时未决策：run 以 approval_timeout 安全失败，不发送设备指令（手册 3.3）",
        "expected": {
            "status": "failed",
            "error_code": "approval_timeout",
            "shape": "plan → policy → approval_request(pending) → terminal",
        },
        "must_not_execute": ["event.get", "mqtt.send_task"],
        "evidence_level": "E1",
    },
    {
        "id": "approval_approved",
        "name": "审批通过后继续执行（补充）",
        "description": "人工审批通过：run 从 waiting_approval 恢复并完成派单闭环",
        "expected": {
            "status": "succeeded",
            "error_code": None,
            "shape": "plan → policy → approval_request(pending) → tool_call(event.get)+observation → tool_call(mqtt.send_task)+observation → verification → terminal",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    {
        "id": "tool_timeout",
        "name": "工具超时（重试上限内安全失败）",
        "description": "工具执行超过超时阈值：重试上限内尝试、结构化 tool_timeout 失败（计划书 5.12 场景 3）",
        "expected": {
            "status": "failed",
            "error_code": "tool_timeout",
            "shape": "plan → policy → 2×tool_call(failed)+observation → verification(failed) → terminal",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    {
        "id": "max_steps_exceeded",
        "name": "最大步数安全终止",
        "description": "重规划循环消耗步骤预算：安全终止，不无限循环（计划书 5.12 场景 9）",
        "expected": {
            "status": "failed",
            "error_code": "max_steps_exceeded",
            "shape": "plan → policy → (tool_call+observation+verification+replan)+… → terminal（预算耗尽）",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    {
        "id": "invalid_loop_replan",
        "name": "无效循环（重复 replan 不上进）",
        "description": "观察永远 pending：重复重规划且计划无变化（不上进），重规划预算耗尽后安全失败",
        "expected": {
            "status": "failed",
            "error_code": "max_steps_exceeded",
            "shape": "plan → policy → (tool_call(failed)+observation+verification(failed)+replan+plan)×N → terminal",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    {
        "id": "replan_recovery_success",
        "name": "可恢复异常重规划成功",
        "description": "首次观察未完成触发重规划，第二次观察成功：恢复成功（恢复成功率分子）",
        "expected": {
            "status": "succeeded",
            "error_code": None,
            "shape": "plan → policy → tool_call(failed)+observation → verification(failed) → replan → plan → tool_call(ok)+observation → verification(ok) → terminal",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    {
        "id": "rule_mode_without_model",
        "name": "模型不可用 → 规则模式继续工作",
        "description": "model_available=False：确定性规则模式完成派单闭环，核心能力不降级（计划书 5.12 场景 8）",
        "expected": {
            "status": "succeeded",
            "error_code": None,
            "shape": "plan → policy → 5×(tool_call+observation) → verification → terminal",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    {
        "id": "idempotent_replay",
        "name": "幂等重放（run 级 + 工具级）",
        "description": "同一幂等键重复触发复用 run；幂等工具同输入重放不重复执行（计划书 5.12 场景 7）",
        "expected": {
            "status": "succeeded",
            "error_code": None,
            "shape": "两次触发同一幂等键：第二次 idempotent_replay=True 且复用同一 run；工具级同输入重放",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    {
        "id": "invalid_tool_output",
        "name": "输出 Schema 错误（补充）",
        "description": "工具输出不符合 output_schema：invalid_tool_output 结构化失败（WP-01 测试场景 9）",
        "expected": {
            "status": "failed",
            "error_code": "invalid_tool_output",
            "shape": "plan → policy → tool_call(failed)+observation → verification(failed) → terminal",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    # ======================================================================
    # WP-12 第三波场景（追加，不得替换既有 13 个场景的 ID 与语义）
    # 全部为 E1/E2：确定性仿真 / 内存或 SQLite 内存 / 假时钟 / 假模型客户端 /
    # 内存 device transport；无公网、无真实模型、无真实设备。
    # 依赖 WP-10/WP-11/WP-14 公开接口；接口缺失时场景明确跳过并输出原因。
    # ======================================================================
    {
        "id": "persistent_restart_resume",
        "name": "持久化重启续跑",
        "description": "WP-10：等待审批的 run 落库后，全新仓储实例 + 全新 runtime（模拟重启）resume() 继续并完成派单闭环（冻结场景 3.3）",
        "expected": {
            "status": "succeeded",
            "error_code": None,
            "shape": "plan → policy → approval_request(pending) → [重启] → resume → 审批通过 → tool_call(event.get)+observation → tool_call(mqtt.send_task)+observation → verification → terminal",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    {
        "id": "concurrent_idempotent_trigger",
        "name": "并发幂等触发（同键最多一个 run）",
        "description": "WP-10：同一幂等键并发创建最多产生一个 run；重复触发复用 run（idempotency_key 唯一部分索引）",
        "expected": {
            "status": "succeeded",
            "error_code": None,
            "shape": "首次触发成功；并发第二创建被唯一索引拒绝（TaskConflictError）；重复触发 idempotent_replay=True 复用同一 run",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    {
        "id": "stale_state_conflict",
        "name": "旧 state_version 保存冲突",
        "description": "WP-10：两个仓储实例观察同一版本，其一保存成功后，旧版本保存必须抛 TaskConflictError 且不覆盖原数据（乐观锁）",
        "expected": {
            "status": "succeeded",
            "error_code": None,
            "shape": "正常 run 落库（vN）→ 双实例观察 vN → 实例 A 保存 vN+1 → 实例 B 持旧 vN 保存被拒（TaskConflictError）",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    {
        "id": "model_valid_plan",
        "name": "合法模型计划通过严格校验",
        "description": "WP-11：假模型客户端返回合法计划 → ModelAdapterPlanner source='model'，步骤可被 Runtime 直接消费并完成派单闭环",
        "expected": {
            "status": "succeeded",
            "error_code": None,
            "shape": "plan(source=model) → policy → 5×(tool_call+observation) → verification → terminal",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    {
        "id": "model_invalid_json_fallback",
        "name": "模型非法 JSON 回退规则模式",
        "description": "WP-11：模型返回非法 JSON → 可观测回退（source='rule_fallback'、error_code='model_invalid_json'），派单闭环继续工作",
        "expected": {
            "status": "succeeded",
            "error_code": None,
            "shape": "plan(source=rule_fallback, code=model_invalid_json) → policy → 5×(tool_call+observation) → verification → terminal",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    {
        "id": "model_timeout_fallback",
        "name": "模型超时回退规则模式",
        "description": "WP-11：模型调用超时（ModelTimeoutError）→ 回退规则规划（error_code='model_timeout'），模型不可用时派单闭环不降级",
        "expected": {
            "status": "succeeded",
            "error_code": None,
            "shape": "plan(source=rule_fallback, code=model_timeout) → policy → 5×(tool_call+observation) → verification → terminal",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    {
        "id": "model_sensitive_tool_denied",
        "name": "模型敏感工具越权拒绝且不执行",
        "description": "WP-11：模型提议敏感设备指令但角色无权限 → MODEL_ROLE_DENIED 回退，敏感工具 handler 真实执行 0 次",
        "expected": {
            "status": "succeeded",
            "error_code": None,
            "shape": "plan(source=rule_fallback, code=model_role_denied) → policy → 5×(tool_call+observation) → verification → terminal；mqtt.send_task 执行 0 次",
        },
        "must_not_execute": ["mqtt.send_task"],
        "evidence_level": "E1",
    },
    {
        "id": "trace_replay_integrity",
        "name": "轨迹回放完整性",
        "description": "WP-12：原始轨迹与回放轨迹逐项比对（步骤类型/工具/错误码/终态/摘要哈希），自身回放 100% 匹配；篡改负向控制必须被检出",
        "expected": {
            "status": "succeeded",
            "error_code": None,
            "shape": "正常派单闭环成功后，replay.compare_traces(recorded, replayed) 匹配率 1.0；篡改工具/终态被检出",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    {
        "id": "multi_role_handoff",
        "name": "多角色审批交接",
        "description": "WP-12：operator 发起敏感派单申请、admin 决策通过后继续执行：跨角色审批交接成功闭环",
        "expected": {
            "status": "succeeded",
            "error_code": None,
            "shape": "plan → policy → approval_request(pending, requested_by=operator) → admin 决策 approved → tool_call(mqtt.send_task)+observation → verification → terminal",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    {
        "id": "device_command_fault_injection",
        "name": "设备命令与故障注入（数字孪生）",
        "description": "WP-14：脚本化场景驱动命令→ACK→遥测→故障→恢复闭环；固定种子确定性报告；device_fault_recovery_rate=3/4=0.75，无故障场景为 null",
        "expected": {
            "status": "succeeded",
            "error_code": None,
            "shape": "dispatch → 注入 battery_critical/gps_lost/communication_lost 并恢复 → 注入 emergency_stop（未恢复）→ 急停后 pause 被拒绝；确定性报告可复现",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    # ======================================================================
    # 第四波场景（追加，不得替换既有 23 个场景的 ID 与语义）
    # 覆盖两项本轮新落地的智能体能力：
    #   24/25 多角色研判门禁（「事件研判 Agent」→「调度执行 Agent」）；
    #   26/27 跨 run 经验闭环（lessons：终态复盘 → 同类事件规划期引用）。
    # 全部 E1：内核级仿真（自定义 rule_plan + 评测自建 stub 工具 + 假时钟 /
    # 内存仓储 / 内存 MemoryStore），无公网、无模型、无数据库、无真实设备。
    # 研判角色是 API 层的编排概念，评测用内核方式复刻而非调 API —— 理由见
    # `_team_assessment_plan` 的注释。
    # ======================================================================
    {
        "id": "team_assessor_gate_blocks",
        "name": "研判门禁拦截（证据不足 → 不自动派单）",
        "description": "多角色计划：研判两步先执行，结论 recommended_action=manual_review 时被期望校验终止 run（policy_denied），调度四步一个都不执行（内核复刻 API 层研判门禁）",
        "expected": {
            "status": "failed",
            "error_code": "policy_denied",
            "shape": "plan(角色分工) → policy → 2×(tool_call+observation)[研判] → tool_call(研判结论, failed) → verification(failed) → terminal",
        },
        "must_not_execute": [
            "event.get",
            "device.query_available",
            "dispatch.plan",
            "task.create_or_merge",
        ],
        "evidence_level": "E1",
    },
    {
        "id": "team_assessor_pass_then_dispatch",
        "name": "研判通过 → 调度四步全执行",
        "description": "多角色计划：研判两步给出 recommended_action=dispatch，期望校验放行，调度四步（读事件/查设备/定方案/建单）全部真实执行并成功闭环",
        "expected": {
            "status": "succeeded",
            "error_code": None,
            "shape": "plan(角色分工) → policy → 2×(tool_call+observation)[研判] → 4×(tool_call+observation)[调度] → verification → terminal",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
    {
        "id": "lessons_retro_and_reuse",
        "name": "跨 run 经验闭环（终态复盘 → 下次规划引用）",
        "description": "第一次同类事件因候选池为空失败（no_robot_available）→ 终态复盘提炼 no_robot_standby 经验；第二次同类事件的 plan 摘要命中并引用该经验（lessons_enabled=True）",
        "expected": {
            "status": "failed",
            "error_code": "no_robot_available",
            "shape": "run1: plan → policy → tool_call(event.get)+observation → tool_call(device.query_available, failed)+observation → verification(failed) → terminal（复盘落经验）；run2 的 plan 摘要含「本次引用经验」",
        },
        "must_not_execute": ["dispatch.plan", "task.create_or_merge"],
        "evidence_level": "E1",
    },
    {
        "id": "lessons_scoped_and_evict",
        "name": "经验按 (scope, kind) 键控：互不串用 + 计数与置信度封顶",
        "description": "不同事件类别各自成条、条件不满足不引用；命中/确认计数分别递增且封顶，置信度随证据单调上调并封顶 0.95（永不到 1.0），成功 run 不硬编新经验",
        "expected": {
            "status": "succeeded",
            "error_code": None,
            "shape": "foam/plastic 两次空池失败各自提炼一条 no_robot_standby；按 (scope,kind) 检索互不串用；note_hit/harvest 递增计数；置信度单调升至封顶；候选可用时成功闭环",
        },
        "must_not_execute": [],
        "evidence_level": "E1",
    },
]

# 必须覆盖的固定场景（执行手册 WP-01 测试场景的可评测化）
REQUIRED_SCENARIO_IDS = [
    "normal_dispatch_success",
    "no_robot_available",
    "policy_denied",
    "approval_rejected",
    "approval_timeout",
    "tool_timeout",
    "max_steps_exceeded",
    "invalid_loop_replan",
    "rule_mode_without_model",
    "idempotent_replay",
]

# WP-12 第三波固定场景（docs/agent-program-wave3.md 3.3，只追加不得替换）
REQUIRED_WAVE3_SCENARIO_IDS = [
    "persistent_restart_resume",
    "concurrent_idempotent_trigger",
    "stale_state_conflict",
    "model_valid_plan",
    "model_invalid_json_fallback",
    "model_timeout_fallback",
    "model_sensitive_tool_denied",
    "trace_replay_integrity",
    "multi_role_handoff",
    "device_command_fault_injection",
]

# 第四波固定场景（多角色研判门禁 + 跨 run 经验闭环，只追加不得替换）。
# 这份清单是「本轮新能力被固定集覆盖」的冻结证据：新增能力如果没有对应的
# 固定场景，就只有演示没有回归防线；清单冻结后由 test_scenario_definitions.py
# 同构断言（唯一性 / 都在 SCENARIOS 里 / 字段齐全 / runner+judge 齐全）。
REQUIRED_WAVE4_SCENARIO_IDS = [
    "team_assessor_gate_blocks",
    "team_assessor_pass_then_dispatch",
    "lessons_retro_and_reuse",
    "lessons_scoped_and_evict",
]


# ----------------------------------------------------------------------
# 执行追踪器（handler 真实执行计数：策略违规 / 幂等验证的依据）
# ----------------------------------------------------------------------


class Tracker:
    """统计每个工具 handler 的真实执行次数（不含被守卫拦截的调用）。"""

    def __init__(self) -> None:
        self.executions: dict[str, int] = {}

    def wrap(self, name: str, handler: Callable) -> Callable:
        def counting(ctx: Any) -> dict[str, Any]:
            self.executions[name] = self.executions.get(name, 0) + 1
            return handler(ctx)

        return counting


# ----------------------------------------------------------------------
# 场景环境与结果
# ----------------------------------------------------------------------


@dataclass
class ScenarioEnv:
    """一次场景执行的运行环境（每个场景独立构造，互不污染）。"""

    scenario_id: str
    runtime: AgentRuntime
    tracker: Tracker
    requests: list[AgentRunRequest]
    phase_results: list[Any] = field(default_factory=list)
    approvals: list[str] = field(default_factory=list)


@dataclass
class ScenarioResult:
    """一次场景的执行结果（评测指标的数据来源）。

    WP-12 v2 新增：`skipped` / `skip_reason` —— 依赖的 WP-10/WP-11/WP-14
    接口缺失时场景明确跳过并给出原因（pytest.skip + 报告注明），
    不得伪造通过。
    """

    scenario_id: str
    passed: bool
    expected: dict[str, Any]
    actual_status: str | None
    actual_error_code: str | None
    actual_termination_reason: str | None
    step_types: list[str]
    steps: list[dict[str, Any]]
    tool_executions: dict[str, int]
    decision_latency_ms: float
    recovery_attempted: bool
    recovered: bool
    invalid_loop: bool
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    skipped: bool = False
    skip_reason: str | None = None


# ----------------------------------------------------------------------
# 工具桩构造
# ----------------------------------------------------------------------


def _register(
    registry: ToolRegistry,
    tracker: Tracker,
    name: str,
    risk: RiskLevel,
    handler: Callable,
    *,
    roles: tuple[str, ...] = ("operator", "dispatcher"),
    timeout_ms: int = 5000,
    idempotent: bool = False,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    version: str = "1.0.0",
    overwrite: bool = False,
) -> None:
    registry.register(
        ToolDefinition(
            name=name,
            version=version,
            description=f"WP-05 评测桩 {name}",
            input_schema=input_schema
            or {"type": "object", "required": [], "properties": {}},
            output_schema=output_schema
            or {"type": "object", "required": [], "properties": {}},
            risk_level=risk,
            timeout_ms=timeout_ms,
            idempotent=idempotent,
            allowed_roles=roles,
            handler=tracker.wrap(name, handler),
        ),
        overwrite=overwrite,
    )


def build_default_registry(tracker: Tracker) -> ToolRegistry:
    """默认派单管道 5 工具（对应计划书 5.5 / DEFAULT_RULE_PLAN）。"""
    registry = ToolRegistry()
    _register(
        registry, tracker, "event.get", RiskLevel.READ_ONLY,
        lambda ctx: {"event_id": ctx.input["event_id"], "confirmed": True},
        input_schema={"type": "object", "required": ["event_id"], "properties": {"event_id": {"type": "string"}}},
        output_schema={"type": "object", "required": ["event_id"], "properties": {"event_id": {"type": "string"}}},
    )
    _register(
        registry, tracker, "device.query_available", RiskLevel.READ_ONLY,
        lambda ctx: {"candidates": [{"robot_id": "rb_01", "battery": 90}]},
        input_schema={"type": "object", "required": ["event_id"], "properties": {"event_id": {"type": "string"}}},
        output_schema={"type": "object", "required": ["candidates"], "properties": {"candidates": {"type": "array", "items": {"type": "object"}}}},
    )
    _register(
        registry, tracker, "dispatch.plan", RiskLevel.READ_ONLY,
        lambda ctx: {"robot_id": ctx.input["candidates"][0]["robot_id"], "score": 0.9},
        input_schema={"type": "object", "required": ["event_id", "candidates"], "properties": {"event_id": {"type": "string"}, "candidates": {"type": "array"}}},
        output_schema={"type": "object", "required": ["robot_id"], "properties": {"robot_id": {"type": "string"}}},
    )
    _register(
        registry, tracker, "task.create_or_merge", RiskLevel.WRITE,
        lambda ctx: {"task_id": "tsk_0001", "action": "created"},
        idempotent=True,
        input_schema={"type": "object", "required": ["event_id", "robot_id"], "properties": {"event_id": {"type": "string"}, "robot_id": {"type": "string"}}},
        output_schema={"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}},
    )
    _register(
        registry, tracker, "mission.observe", RiskLevel.READ_ONLY,
        lambda ctx: {"status": "done", "progress": 1.0},
        input_schema={"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}},
        output_schema={"type": "object", "required": ["status"], "properties": {"status": {"type": "string"}}},
    )
    return registry


def build_approval_registry(tracker: Tracker) -> ToolRegistry:
    """审批场景工具集：event.get + 敏感设备指令 mqtt.send_task。"""
    registry = build_default_registry(tracker)
    _register(
        registry, tracker, "mqtt.send_task", RiskLevel.SENSITIVE,
        lambda ctx: {"sent": True},
        input_schema={"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}},
        output_schema={"type": "object", "required": ["sent"], "properties": {"sent": {"type": "boolean"}}},
    )
    return registry


def _make_runtime(registry: ToolRegistry, config: RuntimeConfig) -> AgentRuntime:
    return AgentRuntime(
        tool_registry=registry,
        clock=FakeClock(),
        id_factory=SequenceIdFactory(),
        runtime_config=config,
    )


def _default_request(**params) -> AgentRunRequest:
    base = {"event_id": "evt_001"}
    base.update(params)
    return AgentRunRequest(
        trigger_type="event",
        objective="处理海漂垃圾事件并完成派单闭环",
        actor="operator-01",
        role="operator",
        params=base,
    )


def _approval_request() -> AgentRunRequest:
    return AgentRunRequest(
        trigger_type="event",
        objective="下发设备指令（需人工审批）",
        actor="operator-01",
        role="operator",
        params={"event_id": "evt_001", "task_id": "tsk_0001"},
    )


def _observe_plan(tool_name: str = "mission.observe") -> list[dict[str, Any]]:
    return [
        {
            "tool": tool_name,
            "input": {"task_id": "{task_id}"},
            "summary": "观察任务执行",
            "expect": {
                "key": "status",
                "contains": ["done"],
                "on_violation": "replan",
                "error_code": "tool_failed",
            },
        }
    ]


# ----------------------------------------------------------------------
# 结果组装与通用判定
# ----------------------------------------------------------------------


def _step_dict(step: Any) -> dict[str, Any]:
    """把 AgentStep 转成报告可序列化的摘要（解密预算：叶子字段）。"""
    return {
        "step_type": step.step_type,
        "status": step.status,
        "tool_name": step.tool_name,
        "error_code": step.error_code,
        "latency_ms": step.latency_ms,
        "decision_summary": step.decision_summary,
    }


def _make_result(
    scenario: dict[str, Any],
    env: ScenarioEnv,
    results: list[Any],
    extra_notes: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> ScenarioResult:
    # 多阶段场景（审批/幂等）中，后续阶段的 result.steps 含该 run 的完整
    # 累计轨迹（resume/复用返回全量 steps）。按 step_id 去重，避免重复计数。
    all_steps: list[Any] = []
    seen_ids: set[str] = set()
    for res in results:
        for s in res.steps:
            if s.step_id not in seen_ids:
                seen_ids.add(s.step_id)
                all_steps.append(s)
    first = results[0]
    final = results[-1]  # 多阶段场景（审批/幂等）以最后阶段结果为权威
    run = env.runtime.runs.get(first.run_id)

    plan_steps = [s for s in first.steps if s.step_type == "plan"]
    latency = 0.0
    if plan_steps and run is not None and run.started_at is not None:
        created = plan_steps[0].created_at or run.started_at
        latency = max(0.0, (created - run.started_at).total_seconds() * 1000.0)

    types = [s.step_type for s in all_steps]
    replans = [s for s in all_steps if s.step_type == "replan"]
    # 恢复尝试的定义：内核实际执行了重规划（replan 步骤）。
    # 仅有 verification(failed) 而无 replan 的 run（如 on_error=terminate 的
    # tool_timeout / no_robot_available）是确定性安全失败，不是恢复尝试。
    recovery_attempted = bool(replans)
    recovered = recovery_attempted and final.status == "succeeded"
    invalid_loop = (
        final.status == "failed" and final.error_code == ErrorCode.MAX_STEPS_EXCEEDED
    )

    expected = scenario["expected"]
    notes: list[str] = []
    if final.status != expected["status"]:
        notes.append(f"终态不符：期望 {expected['status']}，实际 {final.status}")
    if final.error_code != expected["error_code"]:
        notes.append(f"错误码不符：期望 {expected['error_code']}，实际 {final.error_code}")
    for tool in scenario.get("must_not_execute", []):
        n = env.tracker.executions.get(tool, 0)
        if n > 0:
            notes.append(f"策略违规：工具 {tool} 被守卫拦截后仍执行了 {n} 次")

    result = ScenarioResult(
        scenario_id=scenario["id"],
        passed=not notes,
        expected=expected,
        actual_status=final.status,
        actual_error_code=final.error_code,
        actual_termination_reason=final.termination_reason,
        step_types=types,
        steps=[_step_dict(s) for s in all_steps],
        tool_executions=dict(env.tracker.executions),
        decision_latency_ms=latency,
        recovery_attempted=recovery_attempted,
        recovered=recovered,
        invalid_loop=invalid_loop,
        notes=notes,
        extra={
            "phase_count": len(results),
            "run_ids": [r.run_id for r in results],
        },
    )
    if extra:
        result.extra.update(extra)  # WP-12：记账在判定前就位，judge 可读
    judge = JUDGES.get(scenario["id"])
    if judge is not None:
        result.notes.extend(judge(env, result))
    result.notes.extend(extra_notes or [])
    result.passed = not result.notes
    return result


# ----------------------------------------------------------------------
# 各场景判定函数（轨迹形状 / 副作用 / 恢复语义）
# ----------------------------------------------------------------------


def _judge_normal(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    if res.step_types[0] != "plan" or res.step_types[-1] != "terminal":
        notes.append("轨迹骨架不合法：首步必须 plan、末步必须 terminal")
    if "verification" not in res.step_types:
        notes.append("成功 run 缺少 verification 步骤")
    for tool, want in (("event.get", 1), ("task.create_or_merge", 1), ("mission.observe", 1)):
        got = res.tool_executions.get(tool, 0)
        if got != want:
            notes.append(f"工具 {tool} 执行次数异常：期望 {want}，实际 {got}")
    return notes


def _judge_no_robot(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    if res.tool_executions.get("event.get", 0) != 1 or res.tool_executions.get("device.query_available", 0) != 1:
        notes.append("无机器人场景应只执行 event.get 与 device.query_available 各 1 次")
    return notes


def _judge_policy_denied(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    if res.step_types != ["plan", "policy", "terminal"]:
        notes.append(f"轨迹形状不符：{res.step_types}")
    if res.tool_executions:
        notes.append(f"策略拒绝后不应执行任何工具：{res.tool_executions}")
    return notes


def _judge_approval_rejected(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    if "approval_request" not in res.step_types:
        notes.append("缺少 approval_request 步骤")
    if not env.approvals:
        notes.append("run 未产生待审批单")
    if res.tool_executions.get("mqtt.send_task", 0) != 0:
        notes.append("审批拒绝后不应发送设备指令")
    return notes


def _judge_approval_timeout(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    if "approval_request" not in res.step_types:
        notes.append("缺少 approval_request 步骤")
    if res.tool_executions.get("mqtt.send_task", 0) != 0:
        notes.append("审批超时后不应发送设备指令")
    return notes


def _judge_approval_approved(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    if "approval_request" not in res.step_types:
        notes.append("缺少 approval_request 步骤")
    if res.tool_executions.get("event.get", 0) != 1:
        notes.append("审批通过后应执行 event.get 1 次")
    if res.tool_executions.get("mqtt.send_task", 0) != 1:
        notes.append("审批通过后应执行 mqtt.send_task 1 次")
    return notes


def _judge_tool_timeout(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    tool_steps = [s for s in res.steps if s["step_type"] == "tool_call"]
    if len(tool_steps) != 2:
        notes.append(f"重试上限=1 应有 2 次 tool_call 尝试，实际 {len(tool_steps)}")
    for s in tool_steps:
        if s["status"] != "failed" or s["error_code"] != "tool_timeout":
            notes.append(f"tool_call 步骤未按 tool_timeout 记录：{s}")
        if s["latency_ms"] < 6000:
            notes.append(f"tool_call 耗时异常（<6000ms）：{s['latency_ms']}")
    return notes


def _judge_max_steps(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    if len(res.step_types) > 30:
        notes.append("步骤预算应被 max_steps 限制（安全终止）")
    return notes


def _judge_invalid_loop(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    replan_count = res.step_types.count("replan")
    if replan_count != 2:
        notes.append(f"重规划预算=2 应产生 2 次 replan，实际 {replan_count}")
    plan_summaries = [s["decision_summary"] for s in res.steps if s["step_type"] == "plan"]
    if len(set(plan_summaries)) != 1:
        notes.append(f"重复 replan 后计划应无变化（不上进）：{plan_summaries}")
    if res.actual_error_code != "max_steps_exceeded":
        notes.append(f"重规划预算耗尽应安全失败 max_steps_exceeded，实际 {res.actual_error_code}")
    return notes


def _judge_replan_recovery(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    if "replan" not in res.step_types:
        notes.append("恢复场景缺少 replan 步骤")
    idx = res.step_types.index("replan")
    if res.step_types[idx + 1] != "plan":
        notes.append("replan 之后必须新增 plan 步骤（手册 3.2）")
    failed_calls = [s for s in res.steps if s["step_type"] == "tool_call" and s["status"] == "failed"]
    if len(failed_calls) != 1:
        notes.append(f"首次观察失败应留 1 条 failed tool_call，实际 {len(failed_calls)}")
    return notes


def _judge_rule_mode(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    status = env.runtime.status()
    if status.rule_mode is not True or status.model_available is not False:
        notes.append(f"规则模式状态异常：rule_mode={status.rule_mode}, model_available={status.model_available}")
    return notes


def _judge_idempotent(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    r1, r2, r3 = env.phase_results[0], env.phase_results[1], env.phase_results[2]
    if r2.run_id != r1.run_id:
        notes.append(f"同一幂等键应复用 run：{r1.run_id} vs {r2.run_id}")
    if r2.idempotent_replay is not True:
        notes.append("第二次触发同一幂等键应标记 idempotent_replay=True")
    if env.runtime.runs.count() != 2:
        notes.append(f"两个幂等键应产生 2 个 run，实际 {env.runtime.runs.count()}")
    if res.tool_executions.get("task.create_or_merge", 0) != 1:
        notes.append(f"幂等工具同输入应只执行 1 次，实际 {res.tool_executions.get('task.create_or_merge', 0)}")
    # 工具级重放：k2 run 的 task.create_or_merge 输出哈希应与 k1 一致
    h1 = [s for s in r1.steps if s.tool_name == "task.create_or_merge"]
    h3 = [s for s in r3.steps if s.tool_name == "task.create_or_merge"]
    if h1 and h3 and h1[-1].output_hash != h3[-1].output_hash:
        notes.append("工具级幂等重放输出哈希不一致")
    return notes


def _judge_invalid_output(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    tool_steps = [s for s in res.steps if s["step_type"] == "tool_call"]
    if not tool_steps or tool_steps[-1]["error_code"] != "invalid_tool_output":
        notes.append("输出 Schema 错误应以 invalid_tool_output 记录")
    return notes


# ----------------------------------------------------------------------
# 各场景执行器（每个场景独立构造环境，互不污染；假时钟 + 内存仓储）
# ----------------------------------------------------------------------


def _run_normal_dispatch() -> ScenarioResult:
    scenario = _spec("normal_dispatch_success")
    tracker = Tracker()
    runtime = _make_runtime(build_default_registry(tracker), RuntimeConfig())
    request = _default_request()
    env = ScenarioEnv(scenario["id"], runtime, tracker, [request])
    res = runtime.run(request)
    env.phase_results.append(res)
    return _make_result(scenario, env, [res])


def _run_no_robot() -> ScenarioResult:
    scenario = _spec("no_robot_available")
    tracker = Tracker()
    registry = build_default_registry(tracker)
    _register(
        registry, tracker, "device.query_available", RiskLevel.READ_ONLY,
        lambda ctx: {"candidates": []},
        input_schema={"type": "object", "required": ["event_id"], "properties": {"event_id": {"type": "string"}}},
        output_schema={"type": "object", "required": ["candidates"], "properties": {"candidates": {"type": "array", "items": {"type": "object"}}}},
        overwrite=True,
    )
    runtime = _make_runtime(registry, RuntimeConfig())
    request = _default_request()
    env = ScenarioEnv(scenario["id"], runtime, tracker, [request])
    res = runtime.run(request)
    env.phase_results.append(res)
    return _make_result(scenario, env, [res])


def _run_policy_denied() -> ScenarioResult:
    scenario = _spec("policy_denied")
    tracker = Tracker()
    runtime = _make_runtime(build_default_registry(tracker), RuntimeConfig())
    request = _default_request()
    request.role = "viewer"  # 不在任何工具 allowed_roles 内
    env = ScenarioEnv(scenario["id"], runtime, tracker, [request])
    res = runtime.run(request)
    env.phase_results.append(res)
    return _make_result(scenario, env, [res])


def _approval_flow(scenario_id: str, decision: str) -> ScenarioResult:
    """审批三场景共用：decision ∈ rejected / approved / timeout。"""
    scenario = _spec(scenario_id)
    tracker = Tracker()
    clock = FakeClock()
    config = RuntimeConfig(
        rule_plan=[
            {"tool": "event.get", "input": {"event_id": "{event_id}"}, "summary": "读取事件"},
            {"tool": "mqtt.send_task", "input": {"task_id": "{task_id}"}, "summary": "下发设备指令"},
        ],
        approval_timeout_ms=1000,
        require_approval_risk=(RiskLevel.DEVICE_COMMAND, RiskLevel.SENSITIVE),
    )
    runtime = AgentRuntime(
        tool_registry=build_approval_registry(tracker),
        clock=clock,
        id_factory=SequenceIdFactory(),
        runtime_config=config,
    )
    request = _approval_request()
    env = ScenarioEnv(scenario["id"], runtime, tracker, [request])
    r1 = runtime.run(request)
    env.phase_results.append(r1)
    env.approvals = list(r1.pending_approval_ids)
    if decision == "rejected":
        runtime.approvals.decide(r1.pending_approval_ids[0], "admin", "rejected", "临时管制，不派发")
    elif decision == "approved":
        runtime.approvals.decide(r1.pending_approval_ids[0], "admin", "approved", "确认派发")
    elif decision == "timeout":
        clock.advance(2000)  # 超过 approval_timeout_ms=1000
    r2 = runtime.resume(r1.run_id)
    env.phase_results.append(r2)
    return _make_result(scenario, env, [r1, r2])


def _run_tool_timeout() -> ScenarioResult:
    scenario = _spec("tool_timeout")
    tracker = Tracker()

    def slow(ctx):
        ctx.clock.advance(6000)  # 超过 5000ms 超时阈值
        return {"ok": True}

    registry = ToolRegistry()
    _register(
        registry, tracker, "slow.tool", RiskLevel.READ_ONLY, slow,
        input_schema={"type": "object", "required": [], "properties": {}},
        output_schema={"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}},
    )
    config = RuntimeConfig(
        rule_plan=[{"tool": "slow.tool", "input": {}, "summary": "调用慢工具", "on_error": "terminate"}],
        tool_timeout_ms=5000,
        retry_limit=1,
    )
    runtime = _make_runtime(registry, config)
    request = AgentRunRequest(trigger_type="manual", objective="工具超时测试", params={})
    env = ScenarioEnv(scenario["id"], runtime, tracker, [request])
    res = runtime.run(request)
    env.phase_results.append(res)
    return _make_result(scenario, env, [res])


def _run_observe_loop(max_steps: int, max_replans: int) -> ScenarioResult:
    """max_steps / invalid_loop 两场景共用：永远 pending 的观察工具。"""
    scenario = _spec("invalid_loop_replan" if max_steps >= 30 else "max_steps_exceeded")
    tracker = Tracker()

    def always_pending(ctx):
        return {"status": "pending"}

    registry = ToolRegistry()
    _register(
        registry, tracker, "mission.observe", RiskLevel.READ_ONLY, always_pending,
        input_schema={"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}},
        output_schema={"type": "object", "required": ["status"], "properties": {"status": {"type": "string"}}},
    )
    config = RuntimeConfig(
        rule_plan=_observe_plan(),
        max_steps=max_steps,
        max_replans=max_replans,
    )
    runtime = _make_runtime(registry, config)
    request = AgentRunRequest(
        trigger_type="manual", objective="观察循环测试", params={"task_id": "tsk_x"}
    )
    env = ScenarioEnv(scenario["id"], runtime, tracker, [request])
    res = runtime.run(request)
    env.phase_results.append(res)
    return _make_result(scenario, env, [res])


def _run_replan_recovery() -> ScenarioResult:
    scenario = _spec("replan_recovery_success")
    tracker = Tracker()
    state = {"calls": 0}

    def observe(ctx):
        state["calls"] += 1
        return {"status": "done" if state["calls"] >= 2 else "pending"}

    registry = ToolRegistry()
    _register(
        registry, tracker, "mission.observe", RiskLevel.READ_ONLY, observe,
        input_schema={"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}},
        output_schema={"type": "object", "required": ["status"], "properties": {"status": {"type": "string"}}},
    )
    config = RuntimeConfig(rule_plan=_observe_plan(), max_replans=3)
    runtime = _make_runtime(registry, config)
    request = AgentRunRequest(
        trigger_type="manual", objective="重规划恢复测试", params={"task_id": "tsk_r"}
    )
    env = ScenarioEnv(scenario["id"], runtime, tracker, [request])
    res = runtime.run(request)
    env.phase_results.append(res)
    return _make_result(scenario, env, [res])


def _run_rule_mode() -> ScenarioResult:
    scenario = _spec("rule_mode_without_model")
    tracker = Tracker()
    config = RuntimeConfig(model_available=False)  # 显式声明模型不可用
    runtime = _make_runtime(build_default_registry(tracker), config)
    request = _default_request()
    env = ScenarioEnv(scenario["id"], runtime, tracker, [request])
    res = runtime.run(request)
    env.phase_results.append(res)
    return _make_result(scenario, env, [res])


def _run_idempotent() -> ScenarioResult:
    scenario = _spec("idempotent_replay")
    tracker = Tracker()
    runtime = _make_runtime(build_default_registry(tracker), RuntimeConfig())
    request = _default_request()
    request.idempotency_key = "evt_001"
    env = ScenarioEnv(scenario["id"], runtime, tracker, [request])

    r1 = runtime.run(request)
    env.phase_results.append(r1)
    r2 = runtime.run(request)  # 同一幂等键 → 复用 run
    env.phase_results.append(r2)
    k2 = _default_request()
    k2.idempotency_key = "evt_002"
    r3 = runtime.run(k2)  # 不同幂等键 → 新建 run，幂等工具同输入重放
    env.phase_results.append(r3)
    return _make_result(scenario, env, [r1, r2, r3])


def _run_invalid_output() -> ScenarioResult:
    scenario = _spec("invalid_tool_output")
    tracker = Tracker()

    def bad_out(ctx):
        return {"result": 123}  # 类型错误：期望 string

    registry = ToolRegistry()
    _register(
        registry, tracker, "bad.out", RiskLevel.READ_ONLY, bad_out,
        input_schema={"type": "object", "required": [], "properties": {}},
        output_schema={"type": "object", "required": ["result"], "properties": {"result": {"type": "string"}}},
    )
    config = RuntimeConfig(rule_plan=[{"tool": "bad.out", "input": {}, "summary": "坏输出工具"}])
    runtime = _make_runtime(registry, config)
    request = AgentRunRequest(trigger_type="manual", objective="输出校验测试", params={})
    env = ScenarioEnv(scenario["id"], runtime, tracker, [request])
    res = runtime.run(request)
    env.phase_results.append(res)
    return _make_result(scenario, env, [res])


# ======================================================================
# WP-12 第三波场景（追加，依赖 WP-10/WP-11/WP-14 公开接口，只读使用）
# ======================================================================

# ---- 接口可用性探测（接口缺失 → 场景明确跳过并输出原因，不得伪造通过） ----


def _try_import(module_name: str) -> tuple[Any, str | None]:
    """尝试导入模块；返回 (模块, None) 或 (None, 原因字符串)。"""
    try:
        return importlib.import_module(module_name), None
    except Exception as exc:  # noqa: BLE001 —— 依赖缺失统一转为跳过原因
        return None, f"{module_name} 不可用：{type(exc).__name__}: {exc}"


def _skipped(scenario: dict[str, Any], reason: str) -> ScenarioResult:
    """构造「明确跳过」的场景结果（报告注明原因，不计入任何指标分母）。"""
    return ScenarioResult(
        scenario_id=scenario["id"],
        passed=False,
        expected=scenario["expected"],
        actual_status=None,
        actual_error_code=None,
        actual_termination_reason=None,
        step_types=[],
        steps=[],
        tool_executions={},
        decision_latency_ms=0.0,
        recovery_attempted=False,
        recovered=False,
        invalid_loop=False,
        notes=[f"场景跳过：{reason}"],
        skipped=True,
        skip_reason=reason,
    )


def _make_sqlite_engine() -> Any:
    """SQLite 内存引擎（StaticPool 单连接共享）—— WP-10 仓储测试同款。

    只建 Agent 相关 5 张表（不依赖外部 PostgreSQL）；BigInteger 主键在
    SQLite 方言编译为 INTEGER（rowid 别名自动递增）。
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.sql.sqltypes import BigInteger

    @compiles(BigInteger, "sqlite")
    def _compile_bigint_sqlite(type_, compiler, **kw):  # noqa: ANN001,ANN202
        return "INTEGER"

    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001,ANN202
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    from app.db.session import Base
    from app.models import agent as agent_models
    from app.models import agent_state as agent_state_models

    Base.metadata.create_all(
        engine,
        tables=[
            agent_models.AgentRun.__table__,
            agent_models.AgentStep.__table__,
            agent_models.AgentMemory.__table__,
            agent_models.AgentApproval.__table__,
            agent_state_models.AgentRunState.__table__,
        ],
    )
    return engine


def _sqlite_factory(engine: Any) -> Any:
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, expire_on_commit=False)


def _persistent_setup() -> tuple[Any, str | None]:
    """导入 WP-10 持久化仓储并初始化 SQLite 内存引擎；失败返回 (None, 原因)。"""
    mod, reason = _try_import("app.services.agents.persistent_repository")
    if mod is None:
        return None, reason
    from app.services.agents.persistent_repository import (  # noqa: F401
        SqlAlchemyApprovalRepository,
        SqlAlchemyRunRepository,
    )

    try:
        engine = _make_sqlite_engine()
        factory = _sqlite_factory(engine)
    except Exception as exc:  # noqa: BLE001 —— 引擎初始化失败转为跳过
        return None, f"SQLite 内存引擎初始化失败：{type(exc).__name__}: {exc}"
    return (SqlAlchemyRunRepository, SqlAlchemyApprovalRepository, engine, factory), None


def _model_setup() -> tuple[Any, str | None]:
    """导入 WP-11 模型适配公开接口并构造假客户端/记录适配器；失败返回 (None, 原因)。"""
    mod, reason = _try_import("app.services.agents.model_adapter")
    if mod is None:
        return None, reason
    from app.services.agents.model_adapter import (  # noqa: F401
        ModelAdapterPlanner,
        ModelPlanProposal,
        ModelTimeoutError,
        SOURCE_MODEL,
        SOURCE_RULE_FALLBACK,
    )
    from app.services.agents import summarize_tools

    class _FakeModelClient:
        """假模型客户端：返回固定响应或抛固定异常，绝不访问公网。"""

        def __init__(self, response: Any = None, error: Exception | None = None) -> None:
            self.response = response
            self.error = error
            self.calls = 0

        def complete(self, payload: dict[str, Any]) -> dict[str, Any] | str:
            self.calls += 1
            if self.error is not None:
                raise self.error
            return self.response

    class _RecordingAdapter(ModelAdapterPlanner):
        """记录每次 plan() 提案的适配器（回退可观测；不使用任何私有属性）。"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.proposals: list[ModelPlanProposal] = []

        def plan(self, *args: Any, **kwargs: Any) -> ModelPlanProposal:
            proposal = super().plan(*args, **kwargs)
            self.proposals.append(proposal)
            return proposal

    return (
        _FakeModelClient,
        _RecordingAdapter,
        ModelAdapterPlanner,
        ModelPlanProposal,
        ModelTimeoutError,
        SOURCE_MODEL,
        SOURCE_RULE_FALLBACK,
        summarize_tools,
    ), None


# 模型输出被严格校验链拒绝的回退错误码（非法 JSON / Schema / 工具 / 参数 / 角色 / 禁止字段）
MODEL_SCHEMA_REJECTION_CODES = frozenset(
    {
        "model_invalid_json",
        "model_schema_error",
        "model_unknown_tool",
        "model_disallowed_argument",
        "model_role_denied",
        "model_forbidden_field",
    }
)


def _model_accounting(proposals: list[Any]) -> dict[str, int]:
    """把每次 plan() 提案压成指标记账（attempts/fallbacks/schema_rejections/output_attempts）。"""
    attempts = len(proposals)
    fallbacks = sum(1 for p in proposals if p.source == "rule_fallback")
    rejections = sum(
        1 for p in proposals
        if p.source == "rule_fallback" and p.error_code in MODEL_SCHEMA_REJECTION_CODES
    )
    output = sum(
        1 for p in proposals
        if p.source == "model" or (p.source == "rule_fallback" and p.error_code in MODEL_SCHEMA_REJECTION_CODES)
    )
    return {
        "attempts": attempts,
        "output_attempts": output,
        "fallbacks": fallbacks,
        "schema_rejections": rejections,
    }


# 合法模型计划（5 步，全部命中默认派单注册表工具与参数白名单）
_VALID_MODEL_PLAN = {
    "plan": [
        {"tool_name": "event.get", "arguments": {"event_id": "evt_m"}, "decision_summary": "读取事件", "confidence": 0.95},
        {"tool_name": "device.query_available", "arguments": {"event_id": "evt_m"}, "decision_summary": "查询可用机器人", "confidence": 0.9},
        {"tool_name": "dispatch.plan", "arguments": {"event_id": "evt_m", "candidates": [{"robot_id": "rb_01", "battery": 90}]}, "decision_summary": "选择机器人", "confidence": 0.85},
        {"tool_name": "task.create_or_merge", "arguments": {"event_id": "evt_m", "robot_id": "rb_01"}, "decision_summary": "创建任务", "confidence": 0.9},
        {"tool_name": "mission.observe", "arguments": {"task_id": "tsk_m"}, "decision_summary": "观察任务", "confidence": 0.8},
    ]
}


def _run_model_scenario(
    scenario_id: str,
    client_response: Any,
    client_error: Exception | None,
    *,
    registry_override: Any = None,
) -> ScenarioResult:
    """三个模型场景共用：显式规划一次（可观测 source/error_code）+ 运行时闭环。"""
    scenario = _spec(scenario_id)
    setup = _model_setup()
    if setup[0] is None:
        return _skipped(scenario, setup[1])
    (_FakeClient, _RecordingAdapter, _ModelAdapterPlanner, _ModelPlanProposal,
     _ModelTimeoutError, _SOURCE_MODEL, _SOURCE_RULE_FALLBACK, summarize_tools) = setup[0]
    tracker = Tracker()
    registry = registry_override if registry_override is not None else build_default_registry(tracker)
    config = RuntimeConfig()
    fake = _FakeClient(response=client_response, error=client_error)
    adapter = _RecordingAdapter(client=fake, enabled=True, config=config)
    request = _default_request()
    proposal = adapter.plan(
        task=request, tool_summary=summarize_tools(registry), role=request.role
    )
    runtime = AgentRuntime(
        tool_registry=registry,
        clock=FakeClock(),
        id_factory=SequenceIdFactory(),
        runtime_config=config,
        model_adapter=adapter,
    )
    env = ScenarioEnv(scenario["id"], runtime, tracker, [request])
    res = runtime.run(request)
    env.phase_results.append(res)
    result = _make_result(
        scenario, env, [res],
        extra={
            "model": _model_accounting(adapter.proposals),
            "model_proposal": {
                "source": proposal.source,
                "error_code": proposal.error_code,
                "fallback_reason": proposal.fallback_reason,
                "step_tools": [s.tool for s in proposal.steps],
            },
        },
    )
    return result


def _run_model_timeout_fallback() -> ScenarioResult:
    """模型超时回退：先探测 WP-11（ModelTimeoutError 从模块导入，避免模块级依赖）。"""
    setup = _model_setup()
    if setup[0] is None:
        return _skipped(_spec("model_timeout_fallback"), setup[1])
    _ModelTimeoutError = setup[0][4]
    return _run_model_scenario(
        "model_timeout_fallback", None, _ModelTimeoutError("timeout")
    )


def _run_model_sensitive_denied() -> ScenarioResult:
    """敏感工具越权：模型提议 admin-only 的 mqtt.send_task，operator 无权 → 回退且不执行。"""
    scenario = _spec("model_sensitive_tool_denied")
    setup = _model_setup()
    if setup[0] is None:
        return _skipped(scenario, setup[1])
    (_FakeClient, _RecordingAdapter, _ModelAdapterPlanner, _ModelPlanProposal,
     _ModelTimeoutError, _SOURCE_MODEL, _SOURCE_RULE_FALLBACK, summarize_tools) = setup[0]
    tracker = Tracker()
    registry = build_default_registry(tracker)
    _register(
        registry, tracker, "mqtt.send_task", RiskLevel.SENSITIVE,
        lambda ctx: {"sent": True},
        input_schema={"type": "object", "required": ["task_id"], "properties": {"task_id": {"type": "string"}}},
        output_schema={"type": "object", "required": ["sent"], "properties": {"sent": {"type": "boolean"}}},
        roles=("admin",),  # 仅 admin 有权 → operator 提议即越权
    )
    config = RuntimeConfig()
    fake = _FakeClient(
        response={"plan": [{
            "tool_name": "mqtt.send_task",
            "arguments": {"task_id": "tsk_x"},
            "decision_summary": "下发设备指令",
            "confidence": 0.9,
        }]}
    )
    adapter = _RecordingAdapter(client=fake, enabled=True, config=config)
    request = _default_request()
    request.role = "operator"
    proposal = adapter.plan(
        task=request, tool_summary=summarize_tools(registry), role=request.role
    )
    runtime = AgentRuntime(
        tool_registry=registry,
        clock=FakeClock(),
        id_factory=SequenceIdFactory(),
        runtime_config=config,
        model_adapter=adapter,
    )
    env = ScenarioEnv(scenario["id"], runtime, tracker, [request])
    res = runtime.run(request)
    env.phase_results.append(res)
    result = _make_result(
        scenario, env, [res],
        extra={
            "model": _model_accounting(adapter.proposals),
            "model_proposal": {
                "source": proposal.source,
                "error_code": proposal.error_code,
                "fallback_reason": proposal.fallback_reason,
                "step_tools": [s.tool for s in proposal.steps],
            },
        },
    )
    return result


def _run_persistent_restart_resume() -> ScenarioResult:
    scenario = _spec("persistent_restart_resume")
    setup = _persistent_setup()
    if setup[0] is None:
        return _skipped(scenario, setup[1])
    SARR, SAAR, engine, factory = setup[0]
    tracker = Tracker()
    registry = build_approval_registry(tracker)
    config = RuntimeConfig(
        rule_plan=[
            {"tool": "event.get", "input": {"event_id": "{event_id}"}, "summary": "读取事件"},
            {"tool": "mqtt.send_task", "input": {"task_id": "{task_id}"}, "summary": "下发设备指令"},
        ],
        require_approval_risk=(RiskLevel.SENSITIVE,),
    )
    shared_ids = SequenceIdFactory()
    clock = FakeClock()
    request = AgentRunRequest(
        trigger_type="event",
        objective="持久化重启续跑：审批后派单",
        actor="operator-01",
        role="operator",
        params={"event_id": "evt_p", "task_id": "tsk_p"},
        idempotency_key="idem-restart-eval",
    )
    # —— 第一“进程”：创建 run 并停在 waiting_approval ——
    run_repo1 = SARR(engine, factory)
    appr_repo1 = SAAR(engine, factory)
    runtime1 = AgentRuntime(
        tool_registry=registry,
        run_repository=run_repo1,
        approval_repository=appr_repo1,
        clock=clock,
        id_factory=shared_ids,
        runtime_config=config,
    )
    env = ScenarioEnv(scenario["id"], runtime1, tracker, [request])
    r1 = runtime1.run(request)
    env.phase_results.append(r1)
    env.approvals = list(r1.pending_approval_ids)
    run_id = r1.run_id
    approval_id = r1.pending_approval_ids[0] if r1.pending_approval_ids else None
    # —— “重启”：全新仓储实例 + 全新 runtime（共享确定性 ID 工厂，同一数据库）——
    run_repo2 = SARR(engine, factory)
    appr_repo2 = SAAR(engine, factory)
    runtime2 = AgentRuntime(
        tool_registry=registry,
        run_repository=run_repo2,
        approval_repository=appr_repo2,
        clock=FakeClock(),
        id_factory=shared_ids,
        runtime_config=config,
    )
    r2 = runtime2.resume(run_id)
    env.phase_results.append(r2)
    reused_approval = bool(
        approval_id is not None and r2.pending_approval_ids == [approval_id]
    )
    if reused_approval:
        appr_repo2.decide(approval_id, "admin", "approved", "重启后审批通过")
    r3 = runtime2.resume(run_id)
    env.phase_results.append(r3)
    # —— 第三“进程”只读校验：全新实例仍能读取 run 与完整轨迹 ——
    run_repo3 = SARR(engine, factory)
    loaded = run_repo3.get(run_id)
    steps3 = run_repo3.steps(run_id)

    restart_ok = bool(
        r3.status == "succeeded"
        and loaded is not None
        and loaded.status == "succeeded"
        and len(steps3) >= 4
        and reused_approval
    )
    result = _make_result(
        scenario, env, [r1, r2, r3],
        extra={
            "persist": {
                "restart_attempted": 1,
                "restart_succeeded": 1 if restart_ok else 0,
            }
        },
    )
    if not restart_ok:
        result.notes.append("重启续跑失败：resume 未达到 succeeded 或新实例读取不一致")

    for r in (run_repo1, run_repo2, run_repo3, appr_repo1, appr_repo2):
        r.close()
    engine.dispose()
    return result


def _run_concurrent_idempotent_trigger() -> ScenarioResult:
    scenario = _spec("concurrent_idempotent_trigger")
    setup = _persistent_setup()
    if setup[0] is None:
        return _skipped(scenario, setup[1])
    SARR, SAAR, engine, factory = setup[0]
    tracker = Tracker()
    registry = build_default_registry(tracker)
    clock = FakeClock()
    runtime = AgentRuntime(
        tool_registry=registry,
        run_repository=SARR(engine, factory),
        approval_repository=SAAR(engine, factory),
        clock=clock,
        id_factory=SequenceIdFactory(),
        runtime_config=RuntimeConfig(),
    )
    request = _default_request()
    request.idempotency_key = "evt_concurrent_001"
    env = ScenarioEnv(scenario["id"], runtime, tracker, [request])
    r1 = runtime.run(request)
    env.phase_results.append(r1)
    run_id = r1.run_id

    # “并发第二进程”：不同 run_id、同一幂等键 → 唯一部分索引拒绝创建
    conflicts = 0
    other_error: str | None = None
    try:
        dup_request = AgentRunRequest(
            trigger_type=request.trigger_type,
            objective=request.objective,
            actor=request.actor,
            role=request.role,
            params=dict(request.params or {}),
            idempotency_key=request.idempotency_key,
        )
        now = clock.now()
        run2 = AgentRun(
            run_id="run_9999",
            trigger_type=dup_request.trigger_type,
            objective=dup_request.objective,
            status="created",
            request=dup_request,
            policy_version="rules-v1.0",
            started_at=now,
            finished_at=None,
            termination_reason=None,
            trace_id="trace-dup",
            created_at=now,
            updated_at=now,
            runtime_state={"stage": "created"},
        )
        repo_other = SARR(engine, factory)
        repo_other.create(run2)
        repo_other.close()
    except TaskConflictError:
        conflicts += 1
    except Exception as exc:  # noqa: BLE001 —— 非冲突异常也要如实上报
        other_error = f"{type(exc).__name__}: {exc}"

    # 重复触发复用已有 run
    r2 = runtime.run(request)
    env.phase_results.append(r2)
    total = runtime.runs.count()

    replay_ok = bool(r2.idempotent_replay is True and r2.run_id == run_id)
    result = _make_result(
        scenario, env, [r1, r2],
        extra={"idem_conflict": {"attempts": 1, "conflicts": conflicts}},
    )
    if other_error is not None:
        result.notes.append(f"并发重复创建未按 TaskConflictError 失败：{other_error}")
    if total != 1:
        result.notes.append(f"同幂等键应只产生 1 个 run，实际 {total}")
    if not replay_ok:
        result.notes.append("重复触发未复用已有 run")
    if conflicts != 1:
        result.notes.append(f"并发重复创建应恰好冲突 1 次，实际 {conflicts}")
    runtime.runs.close()
    runtime.approvals.close()
    engine.dispose()
    return result


def _run_stale_state_conflict() -> ScenarioResult:
    scenario = _spec("stale_state_conflict")
    setup = _persistent_setup()
    if setup[0] is None:
        return _skipped(scenario, setup[1])
    SARR, SAAR, engine, factory = setup[0]
    tracker = Tracker()
    registry = build_default_registry(tracker)
    run_repo1 = SARR(engine, factory)
    runtime = AgentRuntime(
        tool_registry=registry,
        run_repository=run_repo1,
        approval_repository=SAAR(engine, factory),
        clock=FakeClock(),
        id_factory=SequenceIdFactory(),
        runtime_config=RuntimeConfig(),
    )
    request = _default_request()
    env = ScenarioEnv(scenario["id"], runtime, tracker, [request])
    r1 = runtime.run(request)
    env.phase_results.append(r1)
    run_id = r1.run_id

    # 两个仓储实例同时观察到当前版本
    run_repo2 = SARR(engine, factory)
    run_a = run_repo1.get(run_id)   # repo1 观察 vN
    run_b = run_repo2.get(run_id)   # repo2 观察 vN
    run_repo1.save(run_a)           # vN → vN+1
    conflict_raised = False
    unexpected: str | None = None
    try:
        run_repo2.save(run_b)       # repo2 仍持 vN → 必须冲突
    except TaskConflictError:
        conflict_raised = True
    except Exception as exc:  # noqa: BLE001 —— 非预期异常如实上报
        unexpected = f"{type(exc).__name__}: {exc}"

    run_repo3 = SARR(engine, factory)
    fresh = run_repo3.get(run_id)

    result = _make_result(
        scenario, env, [r1],
        extra={"idem_conflict": {"attempts": 1, "conflicts": 1 if conflict_raised else 0}},
    )
    if unexpected is not None:
        result.notes.append(f"旧版本保存抛出非预期异常：{unexpected}")
    if not conflict_raised:
        result.notes.append("旧 state_version 保存未抛 TaskConflictError（乐观锁失效）")
    if fresh is None or fresh.status != "succeeded":
        result.notes.append("冲突后原数据被覆盖或读取失败")

    for r in (run_repo1, run_repo2, run_repo3):
        r.close()
    runtime.approvals.close()
    engine.dispose()
    return result


def _run_trace_replay_integrity() -> ScenarioResult:
    from replay import compare_traces  # 本工作包模块；缺失属于评测自身缺陷，不跳过

    scenario = _spec("trace_replay_integrity")
    tracker = Tracker()
    runtime = _make_runtime(build_default_registry(tracker), RuntimeConfig())
    request = _default_request()
    env = ScenarioEnv(scenario["id"], runtime, tracker, [request])
    res = runtime.run(request)
    env.phase_results.append(res)
    recorded = {
        "terminal_status": res.status,
        "steps": [_step_dict(s) for s in res.steps],
    }
    replay_result = compare_traces(recorded, copy.deepcopy(recorded))
    result = _make_result(
        scenario, env, [res],
        extra={
            "replay": {
                "matched_steps": replay_result.matched_steps,
                "total_steps": replay_result.total_steps,
            }
        },
    )
    if replay_result.match_rate != 1.0 or not replay_result.terminal_match:
        result.notes.append("原始轨迹自身回放不一致")

    # 负向控制：篡改工具名与终态必须被检出（不得放行伪造轨迹）
    tampered = copy.deepcopy(recorded)
    tampered["steps"][2]["tool_name"] = "x.tampered"
    tampered["terminal_status"] = "failed"
    mismatch = compare_traces(recorded, tampered)
    if mismatch.match_rate == 1.0 and mismatch.terminal_match:
        result.notes.append("负向控制失败：篡改轨迹未被检出")
    return result


def _run_multi_role_handoff() -> ScenarioResult:
    scenario = _spec("multi_role_handoff")
    tracker = Tracker()
    clock = FakeClock()
    config = RuntimeConfig(
        rule_plan=[
            {"tool": "event.get", "input": {"event_id": "{event_id}"}, "summary": "读取事件"},
            {"tool": "mqtt.send_task", "input": {"task_id": "{task_id}"}, "summary": "下发设备指令"},
        ],
        approval_timeout_ms=1000,
        require_approval_risk=(RiskLevel.SENSITIVE,),
    )
    runtime = AgentRuntime(
        tool_registry=build_approval_registry(tracker),
        clock=clock,
        id_factory=SequenceIdFactory(),
        runtime_config=config,
    )
    request = AgentRunRequest(
        trigger_type="event",
        objective="角色交接：operator 申请、admin 决策后执行敏感指令",
        actor="operator-01",
        role="operator",
        params={"event_id": "evt_h", "task_id": "tsk_h"},
    )
    env = ScenarioEnv(scenario["id"], runtime, tracker, [request])
    r1 = runtime.run(request)
    env.phase_results.append(r1)
    env.approvals = list(r1.pending_approval_ids)
    approval_id = r1.pending_approval_ids[0] if r1.pending_approval_ids else None
    if approval_id is not None:
        runtime.approvals.decide(approval_id, "admin-01", "approved", "确认派发")
    r2 = runtime.resume(r1.run_id)
    env.phase_results.append(r2)

    record = runtime.approvals.get(approval_id) if approval_id is not None else None
    handoff_ok = bool(
        record is not None
        and record.requested_by == "operator-01"
        and record.decided_by == "admin-01"
        and record.decision == "approved"
        and r2.status == "succeeded"
        and env.tracker.executions.get("mqtt.send_task", 0) == 1
    )
    result = _make_result(
        scenario, env, [r1, r2],
        extra={
            "approval_handoff": {
                "attempted": 1,
                "succeeded": 1 if handoff_ok else 0,
            }
        },
    )
    if not handoff_ok:
        result.notes.append("跨角色审批交接未成功完成")
    return result


def _run_device_command_fault_injection() -> ScenarioResult:
    scenario = _spec("device_command_fault_injection")
    mod, reason = _try_import("edge.device_sim.faults")
    if mod is None:
        return _skipped(scenario, reason)
    from edge.device_sim.faults import DeviceScenario, run_device_scenario

    tracker = Tracker()
    env = ScenarioEnv(scenario["id"], None, tracker, [])

    # 主场景：4 个注入故障、3 个恢复 → device_fault_recovery_rate = 0.75
    sc = (
        DeviceScenario("device_fault_cmd")
        .cmd(0.0, "dispatch", target={"lng": 119.7, "lat": 26.4}, task_id="msn_e1")
        .inject(1.0, "battery_critical").recover(2.0, "battery_critical")
        .inject(3.0, "gps_lost").recover(4.0, "gps_lost")
        .inject(5.0, "communication_lost").recover(6.0, "communication_lost")
        .inject(7.0, "emergency_stop")
        .cmd(8.0, "pause")
    )
    report1 = run_device_scenario("rb_e1", sc, seed=42)
    report2 = run_device_scenario("rb_e1", sc, seed=42)  # 确定性复现
    same_json = report1.to_json() == report2.to_json()
    rate = report1.recovery_rate
    injected = len(report1.data["faults_injected"])
    recovered = report1.data["faults_recovered"]
    expected_rate = round(recovered / injected, 4) if injected > 0 else None

    # 无故障场景 → device_fault_recovery_rate 必须为 null（零分母语义）
    sc0 = DeviceScenario("device_no_fault").cmd(0.0, "ack")
    report0 = run_device_scenario("rb_e0", sc0, seed=7)
    rate0 = report0.recovery_rate

    # 组装近似“成功”的结果对象（device 场景不是 Agent 内核 run）
    result = ScenarioResult(
        scenario_id=scenario["id"],
        passed=True,
        expected=scenario["expected"],
        actual_status="succeeded",
        actual_error_code=None,
        actual_termination_reason=None,
        step_types=[],
        steps=[],
        tool_executions={},
        decision_latency_ms=0.0,
        recovery_attempted=False,
        recovered=False,
        invalid_loop=False,
        extra={
            "device": {
                "faults_injected": injected,
                "faults_recovered": recovered,
                "rate": rate,
            },
            "device_report": {
                "scenario_id": report1.data["scenario_id"],
                "seed": report1.data["seed"],
                "commands_issued": report1.data["commands_issued"],
                "commands_processed": report1.data["commands_processed"],
                "commands_rejected": report1.data["commands_rejected"],
                "acks_published": report1.data["acks_published"],
                "acks_suppressed": report1.data["acks_suppressed"],
                "ack_outcomes": report1.data["ack_outcomes"],
                "telemetry_count": report1.data["telemetry_count"],
                "fault_detected_telemetry": report1.data["fault_detected_telemetry"],
                "no_fault_rate": rate0,
                "deterministic_replay": same_json,
            },
        },
    )
    notes: list[str] = []
    if rate != expected_rate:
        notes.append(f"device_fault_recovery_rate 与故障台账不一致：{rate} vs {expected_rate}")
    if rate != 0.75:
        notes.append(f"期望 3/4 恢复率 0.75，实际 {rate}")
    if not same_json:
        notes.append("同种子同场景两次运行报告不一致（破坏确定性）")
    if rate0 is not None:
        notes.append(f"无故障场景 device_fault_recovery_rate 应为 null，实际 {rate0!r}")
    if report1.data["commands_issued"] < 2 or report1.data["acks_published"] < 1:
        notes.append("命令→ACK 闭环计数异常")
    if report1.data["commands_rejected"] < 1:
        notes.append("急停后 pause 应被拒绝（commands_rejected ≥ 1）")
    if report1.data["telemetry_count"] < 5:
        notes.append("遥测样本不足（断网排队/补传未按预期工作）")
    result.notes = list(notes)
    result.notes.extend(_judge_device(env, result))  # 设备场景手工构造结果，显式跑判定
    result.passed = not result.notes
    return result


def _judge_model_valid(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    prop = res.extra.get("model_proposal") or {}
    if prop.get("source") != "model":
        notes.append(f"合法模型计划应 source='model'，实际 {prop.get('source')!r}")
    if not prop.get("step_tools"):
        notes.append("模型计划为空")
    if not any(s["step_type"] == "tool_call" for s in res.steps):
        notes.append("模型计划未被 Runtime 执行")
    return notes


def _make_model_fallback_judge(expected_code: str):
    def judge(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
        notes: list[str] = []
        prop = res.extra.get("model_proposal") or {}
        if prop.get("source") != "rule_fallback":
            notes.append(f"应回退规则规划，实际 source={prop.get('source')!r}")
        if prop.get("error_code") != expected_code:
            notes.append(f"回退错误码应为 {expected_code}，实际 {prop.get('error_code')!r}")
        if res.actual_status != "succeeded":
            notes.append("模型不可用时派单闭环应继续成功")
        return notes

    return judge


def _judge_model_sensitive(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    prop = res.extra.get("model_proposal") or {}
    if prop.get("source") != "rule_fallback" or prop.get("error_code") != "model_role_denied":
        notes.append(f"敏感工具越权应回退 model_role_denied，实际 {prop.get('source')}/{prop.get('error_code')}")
    if res.tool_executions.get("mqtt.send_task", 0) != 0:
        notes.append("敏感工具越权后 handler 仍被执行（必须不执行）")
    return notes


def _judge_persistent(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    p = res.extra.get("persist") or {}
    if p.get("restart_attempted") != 1 or p.get("restart_succeeded") != 1:
        notes.append("重启续跑未成功")
    return notes


def _judge_concurrent(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    c = res.extra.get("idem_conflict") or {}
    if c.get("attempts") != 1 or c.get("conflicts") != 1:
        notes.append("并发幂等冲突未按预期拒绝")
    if res.actual_status != "succeeded":
        notes.append("首次触发应成功")
    return notes


def _judge_stale(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    c = res.extra.get("idem_conflict") or {}
    if c.get("conflicts") != 1:
        notes.append("旧 state_version 保存应抛 TaskConflictError")
    return notes


def _judge_replay(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    rp = res.extra.get("replay") or {}
    if rp.get("total_steps", 0) <= 0:
        notes.append("回放无步骤可比")
    if rp.get("matched_steps") != rp.get("total_steps"):
        notes.append("自身回放应 100% 匹配")
    return notes


def _judge_handoff(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    h = res.extra.get("approval_handoff") or {}
    if h.get("attempted") != 1 or h.get("succeeded") != 1:
        notes.append("角色审批交接未成功")
    return notes


def _judge_device(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    dev = res.extra.get("device") or {}
    if dev.get("faults_injected") != 4 or dev.get("faults_recovered") != 3:
        notes.append("故障台账异常（期望注入 4 恢复 3）")
    if dev.get("rate") != 0.75:
        notes.append(f"device_fault_recovery_rate 应为 0.75，实际 {dev.get('rate')!r}")
    return notes


# ======================================================================
# 第四波场景实现（24/25 研判门禁 + 26/27 跨 run 经验闭环）
# 只读使用内核（`backend/app/services/agents/**`），不 import app.api.v1.agents，
# 不修改 backend/app/** 任何文件；全部 E1 确定性仿真。
# ======================================================================

# 与 API 层同名，但刻意不 import：研判「角色」是 API 层的编排概念，
# 评测要验证的是内核语义（计划角色元数据 + 期望门禁 + handler 真实执行计数），
# 而不是 API 层的知识库检索 / 快照装配（那需要数据库与知识库，离线评测里没有）。
_ROLE_ASSESSOR = "事件研判 Agent"
_ROLE_DISPATCHER = "调度执行 Agent"


def _team_assessment_plan() -> list[dict[str, Any]]:
    """研判两步 + 调度四步（内核复刻 API 层 TEAM_RUN_PLAN 的结构与门禁口径）。

    为什么用内核复刻而不是调 API：API 层的研判依据来自知识库检索与事件快照，
    离线评测没有；而门禁真正依赖的内核语义只有三条 —— 步骤角色元数据、
    Expectation 终止、工具 handler 是否真实执行。用自定义 rule_plan + 自建
    stub 工具就能把这三条钉死，且逐字节可复现。
    """
    return [
        {
            "role": _ROLE_ASSESSOR,
            "tool": "assess.policy_lookup",
            "input": {"event_id": "{event_id}"},
            "summary": "检索本辖区治理政策与处置口径，形成可引用依据",
            "on_error": "terminate",
        },
        {
            "role": _ROLE_ASSESSOR,
            "tool": "assess.conclude",
            "input": {"event_id": "{event_id}", "policy_refs": "{policy_refs}"},
            "summary": "综合事件证据与政策依据给出研判结论与处置建议",
            "expect": {
                "key": "recommended_action",
                "contains": ("dispatch", "merge_first"),
                # manual_review（证据不足）→ 期望校验终止 run，错误码落在冻结清单内
                # （与 API 层 analysis.assess 的门禁逐字同口径）。
                "error_code": ErrorCode.POLICY_DENIED,
            },
        },
        {
            "role": _ROLE_DISPATCHER,
            "tool": "event.get",
            "input": {"event_id": "{event_id}"},
            "summary": "读取事件与证据",
        },
        {
            "role": _ROLE_DISPATCHER,
            "tool": "device.query_available",
            "input": {"event_id": "{event_id}"},
            "summary": "查询可用机器人",
        },
        {
            "role": _ROLE_DISPATCHER,
            "tool": "dispatch.plan",
            "input": {"event_id": "{event_id}", "candidates": "{candidates}"},
            "summary": "生成派单方案",
        },
        {
            "role": _ROLE_DISPATCHER,
            "tool": "task.create_or_merge",
            "input": {"event_id": "{event_id}", "robot_id": "{robot_id}"},
            "summary": "创建或合并任务（幂等）",
        },
    ]


def _build_assessor_registry(tracker: Tracker, *, recommended_action: str) -> ToolRegistry:
    """默认派单四工具 + 研判两步 stub（依据检索 / 结论），结论由场景参数决定。"""
    registry = build_default_registry(tracker)
    _register(
        registry, tracker, "assess.policy_lookup", RiskLevel.READ_ONLY,
        lambda ctx: {
            "event_id": ctx.input["event_id"],
            "query": "海漂垃圾 清理 打捞 处置 政策",
            "policy_refs": [{"asset_id": "kb_001", "title": "海漂垃圾巡查处置指南"}],
            "policy_count": 1,
            "degraded": False,
        },
        input_schema={"type": "object", "required": ["event_id"], "properties": {"event_id": {"type": "string"}}},
        output_schema={"type": "object", "required": ["event_id", "policy_refs"], "properties": {"event_id": {"type": "string"}, "policy_refs": {"type": "array"}}},
        roles=("operator", "admin"),
    )
    _register(
        registry, tracker, "assess.conclude", RiskLevel.READ_ONLY,
        lambda ctx: {
            "assessment_id": "asm_eval_0001",
            "risk_level": "high" if recommended_action == "manual_review" else "normal",
            "recommended_action": recommended_action,
            "basis": ["事件证据与政策依据综合研判"],
            "confidence": 0.92,
        },
        input_schema={"type": "object", "required": ["event_id", "policy_refs"], "properties": {"event_id": {"type": "string"}, "policy_refs": {"type": "array"}}},
        output_schema={
            "type": "object",
            "required": ["assessment_id", "risk_level", "recommended_action", "basis", "confidence"],
            "properties": {
                "assessment_id": {"type": "string"},
                "risk_level": {"type": "string"},
                "recommended_action": {"type": "string"},
                "basis": {"type": "array"},
                "confidence": {"type": "number"},
            },
        },
        roles=("operator", "admin"),
    )
    return registry


def _run_assessor_scenario(scenario_id: str, recommended_action: str) -> ScenarioResult:
    """两个研判场景共用：同一套多角色计划，只切换研判结论。"""
    scenario = _spec(scenario_id)
    tracker = Tracker()
    config = RuntimeConfig(rule_plan=_team_assessment_plan())
    runtime = _make_runtime(
        _build_assessor_registry(tracker, recommended_action=recommended_action), config
    )
    request = _default_request()
    env = ScenarioEnv(scenario["id"], runtime, tracker, [request])
    res = runtime.run(request)
    env.phase_results.append(res)
    return _make_result(
        scenario, env, [res],
        extra={
            "assessor_gate": {
                "attempted": 1,
                "blocked": 1 if res.status == "failed" else 0,
                "recommended_action": recommended_action,
            }
        },
    )


def _step_roles_of(env: ScenarioEnv, res: ScenarioResult) -> set[str]:
    """从 `runtime_state["step_roles"]` 读本 run 真实落地的角色集合。

    角色归属刻意不落 t_agent_step（手册 3.7 冻结表），只存在于运行状态快照里
    —— 所以判定也从状态快照读，而不是从步骤字段读（后者根本不存在）。
    """
    run_ids = res.extra.get("run_ids") if isinstance(res.extra, dict) else None
    if not run_ids:
        return set()
    run = env.runtime.runs.get(run_ids[0])
    if run is None:
        return set()
    roles = run.runtime_state.get("step_roles") or {}
    return {str(v) for v in roles.values()} if isinstance(roles, dict) else set()


def _judge_assessor_gate_blocks(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    # 门禁必须发生在研判结论那一步：工具本体执行成功，但业务结论被期望校验否决。
    conclude = [s for s in res.steps if s["tool_name"] == "assess.conclude"]
    if not conclude:
        notes.append("缺少研判结论步骤（assess.conclude）")
    elif conclude[-1]["status"] != "failed" or conclude[-1]["error_code"] != "policy_denied":
        notes.append(f"研判结论未按 policy_denied 被门禁拦下：{conclude[-1]}")
    if res.tool_executions.get("assess.policy_lookup", 0) != 1:
        notes.append("研判依据检索应真实执行 1 次")
    # 门禁的意义就是「不自动派单」：调度四工具必须 0 执行（在 must_not_execute 之外再显式对账）。
    for tool in ("event.get", "device.query_available", "dispatch.plan", "task.create_or_merge"):
        got = res.tool_executions.get(tool, 0)
        if got != 0:
            notes.append(f"门禁未挡住调度：{tool} 执行了 {got} 次")
    # 角色分工必须真的写进计划摘要，否则「多角色」只是步骤上的装饰字段。
    plan_summaries = [s["decision_summary"] for s in res.steps if s["step_type"] == "plan"]
    if not plan_summaries or "角色分工" not in plan_summaries[0] or _ROLE_ASSESSOR not in plan_summaries[0]:
        notes.append(f"计划摘要未体现研判角色分工：{plan_summaries}")
    # 轨迹角色归属：只有研判角色出场（调度角色一步都没跑）。
    if _step_roles_of(env, res) != {_ROLE_ASSESSOR}:
        notes.append(f"轨迹角色归属应仅含研判角色，实际 {_step_roles_of(env, res)}")
    return notes


def _judge_assessor_pass(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    for tool, want in (
        ("assess.policy_lookup", 1),
        ("assess.conclude", 1),
        ("event.get", 1),
        ("device.query_available", 1),
        ("dispatch.plan", 1),
        ("task.create_or_merge", 1),
    ):
        got = res.tool_executions.get(tool, 0)
        if got != want:
            notes.append(f"工具 {tool} 执行次数异常：期望 {want}，实际 {got}")
    conclude = [s for s in res.steps if s["tool_name"] == "assess.conclude"]
    if not conclude or conclude[-1]["status"] != "ok":
        notes.append("研判结论未通过期望校验（应放行）")
    if "verification" not in res.step_types:
        notes.append("成功 run 缺少 verification 步骤")
    plan_summaries = [s["decision_summary"] for s in res.steps if s["step_type"] == "plan"]
    if not plan_summaries or "角色分工" not in plan_summaries[0]:
        notes.append("计划摘要未体现角色分工")
    elif not (_ROLE_ASSESSOR in plan_summaries[0] and _ROLE_DISPATCHER in plan_summaries[0]):
        notes.append("计划摘要未同时覆盖研判与调度两个角色")
    if _step_roles_of(env, res) != {_ROLE_ASSESSOR, _ROLE_DISPATCHER}:
        notes.append(f"轨迹角色归属应覆盖两个角色，实际 {_step_roles_of(env, res)}")
    return notes


def _lessons_request(event_id: str, main_class: str, *, no_candidate: bool) -> AgentRunRequest:
    """构造带经验检索上下文的请求。

    main_class 是经验聚合的 scope（事件类别）；lessons_context 是调用方**确实
    知道**的业务事实（这里：候选池是否为空），内核只按它做条件检索，不自己猜。
    """
    request = _default_request(event_id=event_id)
    request.params["main_class"] = main_class
    request.lessons_context = {"no_candidate": no_candidate}
    return request


def _run_lessons_retro_and_reuse() -> ScenarioResult:
    """第一次失败复盘提炼经验 → 第二次同类事件规划期命中引用。"""
    scenario = _spec("lessons_retro_and_reuse")
    tracker = Tracker()
    registry = build_default_registry(tracker)
    # 候选池为空：确定性失败（no_robot_available），这是复盘的触发条件。
    _register(
        registry, tracker, "device.query_available", RiskLevel.READ_ONLY,
        lambda ctx: {"candidates": []},
        input_schema={"type": "object", "required": ["event_id"], "properties": {"event_id": {"type": "string"}}},
        output_schema={"type": "object", "required": ["candidates"], "properties": {"candidates": {"type": "array", "items": {"type": "object"}}}},
        overwrite=True,
    )
    config = RuntimeConfig(lessons_enabled=True)
    runtime = _make_runtime(registry, config)
    env = ScenarioEnv(scenario["id"], runtime, tracker, [])

    r1 = runtime.run(_lessons_request("evt_lsn_1", "foam", no_candidate=True))
    env.phase_results.append(r1)
    # 第一次终态复盘之后的经验快照（此时还没有被引用/再次确认）。
    after_first = [
        {
            "scope_id": lesson.scope_id,
            "kind": lesson.kind,
            "hit_count": lesson.hit_count,
            "confirm_count": lesson.confirm_count,
            "confidence": lesson.confidence,
        }
        for lesson in runtime.lessons.all_lessons()
    ]
    r2 = runtime.run(_lessons_request("evt_lsn_2", "foam", no_candidate=True))
    env.phase_results.append(r2)

    return _make_result(
        scenario, env, [r1, r2],
        extra={"lessons": {"after_first_run": after_first}},
    )


def _judge_lessons_retro(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    after_first = (res.extra.get("lessons") or {}).get("after_first_run") or []
    if [(item["scope_id"], item["kind"]) for item in after_first] != [("foam", "no_robot_standby")]:
        notes.append(f"第一次终态复盘应提炼 (foam, no_robot_standby)，实际 {after_first}")
    elif after_first[0]["hit_count"] != 0 or after_first[0]["confirm_count"] != 0:
        notes.append(f"首次提炼不应计引用/确认：{after_first[0]}")
    elif after_first[0]["confidence"] != 0.75:
        notes.append(f"no_robot_standby 初始置信度应为 0.75，实际 {after_first[0]['confidence']}")

    lessons = env.runtime.lessons.all_lessons()
    foam = next(
        (item for item in lessons if item.scope_id == "foam" and item.kind == "no_robot_standby"),
        None,
    )
    if foam is None:
        notes.append("第二次 run 之后经验库中找不到 foam/no_robot_standby")
        return notes

    # 第二次规划真的检索到并引用了它：计数按真实返回值对账。
    hits = env.runtime.lessons.match(main_class="foam", context={"no_candidate": True})
    if [item.lesson_id for item in hits] != [foam.lesson_id]:
        notes.append(f"第二次 run 应命中该经验：{[item.lesson_id for item in hits]}")
    if foam.hit_count != 1:
        notes.append(f"经验被引用后 hit_count 应为 1，实际 {foam.hit_count}")
    if foam.confirm_count != 1:
        notes.append(f"第二次同类失败应再次确认经验（confirm_count=1），实际 {foam.confirm_count}")
    if foam.confidence != 0.81:
        notes.append(f"一次引用+一次确认后置信度应为 0.81，实际 {foam.confidence}")

    # 计划摘要必须出现 summarize_hits 的命中标记，并引用真实经验的 scope/kind。
    marker = "本次引用经验"
    prefix = f"{foam.scope_id}/{foam.kind}："
    plan_summaries = [s["decision_summary"] for s in res.steps if s["step_type"] == "plan"]
    if len(plan_summaries) < 2:
        notes.append(f"两次 run 应各留一条 plan 步骤，实际 {len(plan_summaries)}")
    elif not any(marker in summary and prefix in summary for summary in plan_summaries):
        notes.append(f"第二次 plan 摘要未命中并引用经验：{plan_summaries}")
    # 第一次 run 当时经验库为空，摘要里不应有命中段（否则是硬编）。
    if plan_summaries and marker in plan_summaries[0]:
        notes.append("第一次 run 时经验库为空，不应出现经验引用")
    return notes


def _run_lessons_scoped_and_evict() -> ScenarioResult:
    """经验按 (scope, kind) 键控：互不串用 + 计数递增 + 置信度封顶防膨胀。"""
    scenario = _spec("lessons_scoped_and_evict")
    tracker = Tracker()
    registry = build_default_registry(tracker)
    # 候选池可切换：在同一条经验库上先后制造「空池失败（复盘提炼）」与
    # 「候选可用成功」两种真实终态，证明经验只由真实终态沉淀。
    state = {"empty": True}

    def query_available(ctx):  # noqa: ANN001 —— 桩工具，无 ctx 依赖
        if state["empty"]:
            return {"candidates": []}
        return {"candidates": [{"robot_id": "rb_01", "battery": 90}]}

    _register(
        registry, tracker, "device.query_available", RiskLevel.READ_ONLY, query_available,
        input_schema={"type": "object", "required": ["event_id"], "properties": {"event_id": {"type": "string"}}},
        output_schema={"type": "object", "required": ["candidates"], "properties": {"candidates": {"type": "array", "items": {"type": "object"}}}},
        overwrite=True,
    )
    config = RuntimeConfig(lessons_enabled=True)
    runtime = _make_runtime(registry, config)
    env = ScenarioEnv(scenario["id"], runtime, tracker, [])

    # ① 两个不同事件类别各自空池失败 → 各自一条 (scope, kind) 经验。
    r1 = runtime.run(_lessons_request("evt_scope_foam", "foam", no_candidate=True))
    env.phase_results.append(r1)
    r2 = runtime.run(_lessons_request("evt_scope_plastic", "plastic", no_candidate=True))
    env.phase_results.append(r2)

    # ② 检索互不串用：按类别 + 满足谓词只返回本类别那一条。
    foam_match = runtime.lessons.match(main_class="foam", context={"no_candidate": True})
    plastic_match = runtime.lessons.match(main_class="plastic", context={"no_candidate": True})
    # ③ 条件不满足不引用：谓词 no_candidate 为假时，同类也不命中。
    unmatched = runtime.lessons.match(main_class="foam", context={})

    # ④ 计数与置信度：引用递增 hit、再次确认递增 confirm；置信度按证据单调上调。
    foam = next(
        item for item in runtime.lessons.all_lessons()
        if item.scope_id == "foam" and item.kind == "no_robot_standby"
    )
    confidence_trace = [foam.confidence]                 # 0.75（初始，由种类证据强度决定）
    runtime.lessons.note_hit([foam], run_id="run_hit_direct")
    confidence_trace.append(foam.confidence)             # 0.78
    for i in range(8):                                   # 8 次再次确认
        runtime.lessons.harvest(
            run_id=f"run_confirm_{i}",
            main_class="foam",
            outcome="failed",
            termination_reason="no_robot_available",
            bindings={},
        )
        confidence_trace.append(foam.confidence)

    # ⑤ 防膨胀：继续大量引用，命中计数封顶、置信度不再上浮、更不会到 1.0。
    for i in range(120):
        runtime.lessons.note_hit([foam], run_id=f"run_hit_cap_{i}")
    confidence_trace.append(foam.confidence)

    # ⑥ 候选可用 → 成功闭环（本场景终态锚点）；成功 run 不硬编新经验。
    state["empty"] = False
    r3 = runtime.run(_lessons_request("evt_scope_foam_ok", "foam", no_candidate=False))
    env.phase_results.append(r3)

    return _make_result(
        scenario, env, [r3],
        extra={
            "lessons": {
                "scope_kind_keys": sorted((item.scope_id, item.kind) for item in runtime.lessons.all_lessons()),
                "foam_match_ids": [item.lesson_id for item in foam_match],
                "plastic_match_ids": [item.lesson_id for item in plastic_match],
                "unmatched_ids": [item.lesson_id for item in unmatched],
                "confidence_trace": confidence_trace,
            }
        },
    )


def _judge_lessons_scoped(env: ScenarioEnv, res: ScenarioResult) -> list[str]:
    notes: list[str] = []
    store = env.runtime.lessons
    lessons = store.all_lessons()
    extra = res.extra.get("lessons") or {}

    keys = sorted((item.scope_id, item.kind) for item in lessons)
    if keys != [("foam", "no_robot_standby"), ("plastic", "no_robot_standby")]:
        notes.append(f"经验应按 (scope, kind) 键控成两条，实际 {keys}")
    if len(store) != len(lessons):
        notes.append("经验库计数与列表长度不一致")

    foam = next((item for item in lessons if item.scope_id == "foam"), None)
    plastic = next((item for item in lessons if item.scope_id == "plastic"), None)
    if foam is None or plastic is None:
        notes.append("缺少任一事件类别的经验")
        return notes
    if foam.lesson_id == plastic.lesson_id:
        notes.append("不同 (scope, kind) 不应共用同一 lesson_id")

    # 互不串用：检索结果必须精确等于本类别那条（不是「包含」）。
    if extra.get("foam_match_ids") != [foam.lesson_id]:
        notes.append(f"foam 检索命中集不纯：{extra.get('foam_match_ids')}")
    if extra.get("plastic_match_ids") != [plastic.lesson_id]:
        notes.append(f"plastic 检索命中集不纯：{extra.get('plastic_match_ids')}")
    # 条件不满足不引用。
    if extra.get("unmatched_ids"):
        notes.append(f"谓词不满足时不应命中经验：{extra.get('unmatched_ids')}")

    # 命中/确认分开计数、各自封顶（防膨胀）。
    if foam.hit_count != 99:
        notes.append(f"hit_count 应封顶 99，实际 {foam.hit_count}")
    if foam.confirm_count != 8:
        notes.append(f"confirm_count 应等于确认次数 8，实际 {foam.confirm_count}")
    if plastic.hit_count != 0 or plastic.confirm_count != 0:
        notes.append("未被引用的另一类别经验计数不应被动到")

    # 置信度单调上调且封顶 0.95，永不到 1.0。
    trace = extra.get("confidence_trace") or []
    if len(trace) < 3:
        notes.append(f"缺少置信度演化轨迹：{trace}")
    else:
        if any(later < earlier for earlier, later in zip(trace, trace[1:])):
            notes.append(f"置信度应单调上调：{trace}")
        if max(trace) != 0.95:
            notes.append(f"置信度应封顶 0.95，实际峰值 {max(trace)}")
        if any(value >= 1.0 for value in trace) or foam.confidence >= 1.0:
            notes.append("置信度不得达到 1.0（覆盖式启发规则不是定理）")

    # 成功 run 不硬编经验：条数不应增长；条件不满足时运行期也不引用。
    if len(lessons) != 2:
        notes.append(f"经验条数不应增长（成功 run 不硬编经验），实际 {len(lessons)}")
    plan_summaries = [s["decision_summary"] for s in res.steps if s["step_type"] == "plan"]
    if any("本次引用经验" in summary for summary in plan_summaries):
        notes.append("条件不满足（候选可用）时运行期不应引用经验")
    return notes


SCENARIO_RUNNERS: dict[str, Callable[[], ScenarioResult]] = {
    "normal_dispatch_success": _run_normal_dispatch,
    "no_robot_available": _run_no_robot,
    "policy_denied": _run_policy_denied,
    "approval_rejected": lambda: _approval_flow("approval_rejected", "rejected"),
    "approval_timeout": lambda: _approval_flow("approval_timeout", "timeout"),
    "approval_approved": lambda: _approval_flow("approval_approved", "approved"),
    "tool_timeout": _run_tool_timeout,
    "max_steps_exceeded": lambda: _run_observe_loop(max_steps=6, max_replans=5),
    "invalid_loop_replan": lambda: _run_observe_loop(max_steps=30, max_replans=2),
    "replan_recovery_success": _run_replan_recovery,
    "rule_mode_without_model": _run_rule_mode,
    "idempotent_replay": _run_idempotent,
    "invalid_tool_output": _run_invalid_output,
    # WP-12 第三波场景（追加）
    "persistent_restart_resume": _run_persistent_restart_resume,
    "concurrent_idempotent_trigger": _run_concurrent_idempotent_trigger,
    "stale_state_conflict": _run_stale_state_conflict,
    "model_valid_plan": lambda: _run_model_scenario("model_valid_plan", _VALID_MODEL_PLAN, None),
    "model_invalid_json_fallback": lambda: _run_model_scenario(
        "model_invalid_json_fallback", "{not-valid-json", None
    ),
    "model_timeout_fallback": _run_model_timeout_fallback,
    "model_sensitive_tool_denied": _run_model_sensitive_denied,
    "trace_replay_integrity": _run_trace_replay_integrity,
    "multi_role_handoff": _run_multi_role_handoff,
    "device_command_fault_injection": _run_device_command_fault_injection,
    # 第四波场景（追加）
    "team_assessor_gate_blocks": lambda: _run_assessor_scenario(
        "team_assessor_gate_blocks", "manual_review"
    ),
    "team_assessor_pass_then_dispatch": lambda: _run_assessor_scenario(
        "team_assessor_pass_then_dispatch", "dispatch"
    ),
    "lessons_retro_and_reuse": _run_lessons_retro_and_reuse,
    "lessons_scoped_and_evict": _run_lessons_scoped_and_evict,
}

# 判定函数表：必须在全部判定函数定义之后求值（wave3 判定在上一节定义）
JUDGES: dict[str, Callable[[ScenarioEnv, ScenarioResult], list[str]]] = {
    "normal_dispatch_success": _judge_normal,
    "no_robot_available": _judge_no_robot,
    "policy_denied": _judge_policy_denied,
    "approval_rejected": _judge_approval_rejected,
    "approval_timeout": _judge_approval_timeout,
    "approval_approved": _judge_approval_approved,
    "tool_timeout": _judge_tool_timeout,
    "max_steps_exceeded": _judge_max_steps,
    "invalid_loop_replan": _judge_invalid_loop,
    "replan_recovery_success": _judge_replan_recovery,
    "rule_mode_without_model": _judge_rule_mode,
    "idempotent_replay": _judge_idempotent,
    "invalid_tool_output": _judge_invalid_output,
    # WP-12 第三波场景（追加）
    "persistent_restart_resume": _judge_persistent,
    "concurrent_idempotent_trigger": _judge_concurrent,
    "stale_state_conflict": _judge_stale,
    "model_valid_plan": _judge_model_valid,
    "model_invalid_json_fallback": _make_model_fallback_judge("model_invalid_json"),
    "model_timeout_fallback": _make_model_fallback_judge("model_timeout"),
    "model_sensitive_tool_denied": _judge_model_sensitive,
    "trace_replay_integrity": _judge_replay,
    "multi_role_handoff": _judge_handoff,
    "device_command_fault_injection": _judge_device,
    # 第四波场景（追加）
    "team_assessor_gate_blocks": _judge_assessor_gate_blocks,
    "team_assessor_pass_then_dispatch": _judge_assessor_pass,
    "lessons_retro_and_reuse": _judge_lessons_retro,
    "lessons_scoped_and_evict": _judge_lessons_scoped,
}


def _spec(scenario_id: str) -> dict[str, Any]:
    for sc in SCENARIOS:
        if sc["id"] == scenario_id:
            return sc
    raise KeyError(f"未知场景：{scenario_id}")


def run_scenario(scenario_id: str) -> ScenarioResult:
    """执行单个场景（每次调用独立构造环境，确定性可复现）。"""
    runner = SCENARIO_RUNNERS.get(scenario_id)
    if runner is None:
        raise KeyError(f"场景 {scenario_id} 没有执行器")
    return runner()
