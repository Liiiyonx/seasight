"""Agent 运行 API（WP-03）—— 把确定性 Agent 内核接入真实业务数据。

本模块只做三件事：
1. 在异步数据库会话中准备一次请求所需的只读业务快照；
2. 同步执行纯内存 Agent 内核（WP-01 `AgentRuntime`，可注入内存实例）；
3. 在运行成功后写入真实工单、事件状态与审计镜像。

运行时本身仍是进程内内存状态；`t_agent_run/t_agent_step/t_agent_approval`
是审计与重启后只读回放镜像，不宣称重启后可以继续执行未完成 run。

冻结契约（Harness 执行手册 3.8）：
    前缀 /api/v1/agents，10 个端点全部走 ApiResponse 信封；
    Agent 业务错误码接 6xxx（见下方错误码块，测试对账守卫锁定）；
    GET /runs 按创建时间倒序分页（PageResult 风格）。

权限边界（测试强制）：
    viewer / 匿名  —— 只读（GET 类全部放行，POST 一律 403）
    operator       —— 可写（创建 run / 取消 run），但不可审批
    admin/approver —— 可决定人工审批（审批续跑按 WP-01 语义推进）
    写操作全部落审计（复用 services/audit.record_audit）。

评测产物隔离：GET /evals/latest 只读取仓库根 `artifacts/agent_evals/latest_v2.json`
（WP-05/WP-12 产出），不写、不扫描其它目录；文件缺失/损坏返回 6008，
绝不伪造指标。v1 的 `latest.json` 仅作为历史产物保留，不再作为在线消费源。
"""

from __future__ import annotations

import contextvars
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import CurrentUser, get_current_user, require_operator
from app.core.exceptions import ApiResponse, AppException, ErrorCode as ApiErrorCode
from app.core.geo import nearest_township
from app.db.session import get_session
from app.models.agent import AgentApproval as AgentApprovalORM
from app.models.agent import AgentRun as AgentRunORM
from app.models.agent import AgentStep as AgentStepORM
from app.models.agent import AgentRunStatus
from app.models.agent_state import AgentRunState as AgentRunStateORM
from app.models.device import Device, DeviceType
from app.models.event import Event, EventStatus, WasteClass
from app.models.task import Task, TaskPriority, TaskStatus
from app.repositories import DeviceRepository, EventRepository, TaskRepository
from app.schemas import (
    AgentApprovalDecision,
    AgentApprovalOut,
    AgentDecisionOut,
    AgentEvalOut,
    AgentLessonOut,
    AgentRunCancel,
    AgentRunCreate,
    AgentRunOut,
    AgentRuntimeStatusOut,
    AgentStepOut,
    AgentToolOut,
    PageMeta,
)
from app.schemas.agent import (
    AgentEvalActualOut,
    AgentEvalExpectedOut,
    AgentEvalMetricsOut,
    AgentEvalScenarioOut,
    AgentRunPageOut,
)
from app.schemas.knowledge import (
    DecisionCreate,
    DecisionEvidenceIn,
    KnowledgeSearchRequest,
)
from app.services.agents import (
    ERROR_CODES,
    TERMINAL_STATUSES,
    AgentRunRequest,
    AgentRuntime,
    AgentStep,
    ErrorCode,
    RiskLevel,
    RuleStep,
    RuntimeConfig,
    TaskConflictError,
    ToolContext,
    ToolDefinition,
    ToolRegistry,
    create_repositories,
)
from app.services.agents.model_adapter import (
    ModelAdapterPlanner,
    OpenAICompatibleModelClient,
)
from app.services.audit import record_audit
from app.services.dispatch import _parse_point, finalize_dispatch
from app.services.knowledge import KnowledgeService

router = APIRouter()


# ----------------------------------------------------------------------
# Agent 专用业务错误码（6xxx，WP-03 冻结，测试对账守卫锁定）
# ----------------------------------------------------------------------
# 手册 3.8「错误码接 6xxx」：以下映射为 WP-03 的穷举设计，测试逐项断言。
#   6001 run 不存在
#   6002 非法状态迁移（run 已终态仍试图推进，如审批续跑）
#   6003 审批不存在 / 已决策 / 决策值非法（不可决定）
#   6004 非运行中不可取消（run 已终态）
#   6005 触发事件不存在
#   6006 事件状态不允许派单
#   6007 业务冲突（该事件已存在活跃工单等）
#   6008 评测产物缺失 / 不可读
#   6009 运行时不可用（保留契约；当前单例懒加载恒可用，异常路径由调用方兜底）
AGENT_RUN_NOT_FOUND = 6001
AGENT_ILLEGAL_STATE_TRANSITION = 6002
AGENT_APPROVAL_NOT_DECIDABLE = 6003
AGENT_RUN_NOT_CANCELLABLE = 6004
AGENT_EVENT_NOT_FOUND = 6005
AGENT_EVENT_NOT_DISPATCHABLE = 6006
AGENT_TASK_CONFLICT = 6007
AGENT_EVAL_UNAVAILABLE = 6008
AGENT_RUNTIME_UNAVAILABLE = 6009

# 审批角色白名单：admin 或明确审批角色（approver）可决定人工审批。
# viewer / operator 一律 403 —— operator 可创建/取消 run，但不可代替人工审批。
APPROVER_ROLES = ("admin", "approver")


def _require_approver(user: CurrentUser) -> CurrentUser:
    """审批权限守卫：仅 admin 或 approver 角色可决定人工审批。"""
    if not (user.is_admin or user.role in APPROVER_ROLES):
        raise AppException(
            code=ApiErrorCode.FORBIDDEN,
            message="当前账号无审批权限（需要 admin 或 approver）",
            http_status=403,
        )
    return user


# ----------------------------------------------------------------------
# 请求级只读快照
# ----------------------------------------------------------------------


@dataclass
class RobotSnapshot:
    """一个候选机器人的请求时快照。"""

    device: Device
    distance_m: float
    battery: int
    bin_usage: float
    same_category_active: bool = False


@dataclass
class AgentEventSnapshot:
    """一次 Agent run 使用的完整业务快照。

    同步工具 handler 只能读这个快照，不直接触碰 AsyncSession。
    """

    event: Event
    lng: float
    lat: float
    candidates: list[RobotSnapshot] = field(default_factory=list)
    conflict_reason: str | None = None
    merge_task_id: str | None = None
    prepared_task: Task | None = None
    selected_robot_id: str | None = None
    # ---- 研判 Agent 的只读依据（仅在多角色计划下加载） ----
    # 政策检索是异步的，工具 handler 是同步的，所以按「异步预取 → 同步只读」
    # 的既有快照模式一次性搬进内存；policy_degraded 为真时表示知识库缺席，
    # 研判退化为事件证据单依据，而不是假装有依据。
    policy_query: str = ""
    policy_refs: list[dict[str, Any]] = field(default_factory=list)
    policy_ontology_version_id: str | None = None
    policy_degraded: bool = False
    policy_note: str | None = None


_SNAPSHOT: contextvars.ContextVar[AgentEventSnapshot | None] = contextvars.ContextVar(
    "seasight_agent_snapshot",
    default=None,
)


def _current_snapshot() -> AgentEventSnapshot:
    snapshot = _SNAPSHOT.get()
    if snapshot is None:
        raise RuntimeError("Agent 工具执行时缺少请求快照")
    return snapshot


async def _load_agent_snapshot(
    session: AsyncSession,
    event_id: str,
) -> AgentEventSnapshot:
    """异步加载一次运行所需的完整只读业务快照。

    政策依据（研判 Agent 用）不在这里取：它只在多角色计划下才需要，
    由调用方显式调 `_prefetch_policy_refs` 追加 —— 这样单角色默认路径
    连一次多余的知识库查询都不产生，也保持本函数的签名稳定。
    """
    event = await EventRepository(session).get_by_event_id(event_id)
    if event is None:
        raise AppException(
            code=AGENT_EVENT_NOT_FOUND,
            message=f"事件 {event_id} 不存在",
            http_status=200,
        )

    row = (
        await session.execute(
            select(func.ST_X(Event.location), func.ST_Y(Event.location)).where(
                Event.event_id == event_id
            )
        )
    ).one_or_none()
    if row is None or row[0] is None or row[1] is None:
        raise AppException(
            code=AGENT_EVENT_NOT_FOUND,
            message=f"事件 {event_id} 缺少有效坐标",
            http_status=200,
        )
    lng, lat = float(row[0]), float(row[1])

    snapshot = AgentEventSnapshot(event=event, lng=lng, lat=lat)
    if event.status != EventStatus.NEW:
        snapshot.conflict_reason = f"事件当前状态为 {event.status}，仅 new 可派单"
        return snapshot

    task_repo = TaskRepository(session)
    if await task_repo.has_active_task_for_event(event_id):
        snapshot.conflict_reason = "该事件已存在活跃工单，拒绝重复建单"
        return snapshot

    mergeable = await task_repo.find_mergeable_task(
        lng=lng,
        lat=lat,
        window_minutes=settings.dispatch_merge_window_minutes,
        radius_m=settings.dispatch_merge_radius_meters,
    )
    snapshot.merge_task_id = mergeable.task_id if mergeable is not None else None

    device_rows = await DeviceRepository(session).find_nearest_available_robots(
        event_id,
        max_distance_m=settings.dispatch_range_meters,
        limit=5,
    )
    candidates: list[RobotSnapshot] = []
    for device, distance_m in device_rows:
        meta = dict(device.meta or {})
        try:
            battery = int(meta.get("battery", 100))
            bin_usage = sum(float(value) for value in (meta.get("bins") or {}).values())
        except (TypeError, ValueError):
            continue

        active_tasks = await task_repo.list_active_tasks_for_robot(device.device_id)
        active_event_ids = [task.event_id for task in active_tasks if task.event_id]
        active_classes = await EventRepository(session).list_main_classes_for_events(
            active_event_ids
        )
        candidates.append(
            RobotSnapshot(
                device=device,
                distance_m=distance_m,
                battery=battery,
                bin_usage=bin_usage,
                same_category_active=event.main_class in active_classes,
            )
        )
    snapshot.candidates = candidates
    return snapshot


# ----------------------------------------------------------------------
# 确定性工具链
# ----------------------------------------------------------------------


def _event_get(ctx: ToolContext) -> dict[str, Any]:
    snapshot = _current_snapshot()
    if ctx.input.get("event_id") != snapshot.event.event_id:
        raise TaskConflictError("工具输入事件与请求快照不一致")
    return {
        "event_id": snapshot.event.event_id,
        "status": snapshot.event.status,
        "main_class": snapshot.event.main_class,
        "lng": snapshot.lng,
        "lat": snapshot.lat,
        "dispatchable": snapshot.conflict_reason is None,
    }


def _device_query_available(ctx: ToolContext) -> dict[str, Any]:
    snapshot = _current_snapshot()
    if ctx.input.get("event_id") != snapshot.event.event_id:
        raise TaskConflictError("工具输入事件与请求快照不一致")

    def _candidate(item: RobotSnapshot) -> dict[str, Any]:
        # 候选坐标一起带出去：大屏地图要按这份快照高亮候选与选中者，
        # 让"为什么派它"在图上看得见，而不是只有轨迹里的一行文字。
        point = _parse_point(item.device.location)
        lng, lat = point if point is not None else (None, None)
        return {
            "robot_id": item.device.device_id,
            "name": item.device.name,
            "lng": lng,
            "lat": lat,
            "distance_m": round(item.distance_m, 2),
            "battery": item.battery,
            "bin_usage": round(item.bin_usage, 4),
            "same_category_active": item.same_category_active,
        }

    return {
        "candidates": [
            _candidate(item)
            for item in snapshot.candidates
            if item.battery >= settings.dispatch_min_battery
            and item.bin_usage < settings.dispatch_max_bin_usage
            and item.distance_m <= settings.dispatch_range_meters
        ],
    }


def _dispatch_plan(ctx: ToolContext) -> dict[str, Any]:
    snapshot = _current_snapshot()
    candidate_ids = [
        str(item.get("robot_id"))
        for item in (ctx.input.get("candidates") or [])
        if isinstance(item, dict) and item.get("robot_id")
    ]
    by_id = {item.device.device_id: item for item in snapshot.candidates}
    usable = [by_id[robot_id] for robot_id in candidate_ids if robot_id in by_id]
    if not usable:
        raise TaskConflictError("快照中没有可用机器人")

    usable.sort(
        key=lambda item: (
            0 if item.same_category_active else 1,
            item.distance_m,
            item.device.device_id,
        )
    )
    selected = usable[0]
    return {
        "robot_id": selected.device.device_id,
        "robot_name": selected.device.name,
        "distance_m": round(selected.distance_m, 2),
        "reason": "同类别顺路优先，其次按距离最近",
    }


def _task_create_or_merge(ctx: ToolContext) -> dict[str, Any]:
    snapshot = _current_snapshot()
    if ctx.input.get("event_id") != snapshot.event.event_id:
        raise TaskConflictError("工具输入事件与请求快照不一致")
    if snapshot.conflict_reason:
        raise TaskConflictError(snapshot.conflict_reason)
    if snapshot.merge_task_id:
        return {
            "task_id": snapshot.merge_task_id,
            "action": "merged",
            "created": False,
        }

    robot_id = str(ctx.input.get("robot_id") or "")
    if robot_id not in {item.device.device_id for item in snapshot.candidates}:
        raise TaskConflictError("派单机器人不在候选快照中")

    task = Task(
        task_id=f"task_{uuid.uuid4().hex[:12]}",
        event_id=snapshot.event.event_id,
        robot_id=robot_id,
        target_location=f"SRID=4326;POINT({snapshot.lng} {snapshot.lat})",
        status=TaskStatus.ASSIGNED,
        priority=TaskPriority.for_waste_class(snapshot.event.main_class),
        township=snapshot.event.township
        or nearest_township(snapshot.lng, snapshot.lat),
        assigned_at=datetime.now(),
    )
    snapshot.prepared_task = task
    snapshot.selected_robot_id = robot_id
    return {"task_id": task.task_id, "action": "created", "created": True}


# ----------------------------------------------------------------------
# 研判 Agent 的两个工具（多角色计划专用）
# ----------------------------------------------------------------------


async def _prefetch_policy_refs(
    session: AsyncSession,
    snapshot: AgentEventSnapshot,
) -> None:
    """为研判 Agent 预取政策依据（知识检索，只读）。

    细节说明（都是刻意的取舍）：
    - `township_scope=None`：县级政策文件（如《连江县海漂垃圾巡查处置指南》）
      在知识库里 township 为空，按乡镇过滤会把最有用的那份政策挡在门外。
      这里只做**只读检索**，取回的证据仅用于写一条 DecisionTrace，
      不改变任何按辖区做权限判定的接口。
    - `asset_types=[document, table]`：只取政策文档与汇总表，
      不把图片/遥测/训练集塞进"政策依据"里。
    - 失败即降级：知识库不可用（表缺失/未灌种子）时置 degraded 并如实说明，
      研判退化为"仅凭事件证据"，绝不阻断派单主链路 —— 与 notify 的降级纪律一致。
    """
    query = " ".join(
        part
        for part in (
            str(snapshot.event.main_class or ""),
            "漂浮垃圾 清理 打捞 处置 政策",
            str(snapshot.event.township or ""),
        )
        if part
    )
    snapshot.policy_query = query

    try:
        _mode, ontology_version_id, hits = await KnowledgeService(session).search(
            KnowledgeSearchRequest(
                query=query,
                hop_depth=2,
                asset_types=["document", "table"],
                limit=5,
            ),
            township_scope=None,
        )
    except Exception as exc:  # noqa: BLE001 —— 研判依据是增强项，失败必须降级而非中断
        snapshot.policy_degraded = True
        snapshot.policy_note = f"知识库检索不可用，研判降级为仅事件证据：{exc}"
        logger.warning(f"[Agent] 研判依据检索失败，降级运行：{exc}")
        return

    snapshot.policy_ontology_version_id = ontology_version_id
    snapshot.policy_refs = [
        {
            "asset_id": hit.asset_id,
            "asset_version_id": hit.asset_version_id,
            "title": hit.title,
            "asset_type": hit.asset_type,
            "source_uri": hit.source_uri,
            "score": round(float(hit.score), 6),
            "citation": (
                hit.citations[0] if hit.citations else (hit.snippet or hit.title)
            ),
            "hop_no": int(hit.hop_count),
            "matched_node_ids": list(hit.matched_node_ids),
            "matched_relation_ids": list(hit.matched_relation_ids),
        }
        for hit in hits
    ]
    if not snapshot.policy_refs:
        snapshot.policy_note = "知识库中未命中相关政策条目，研判仅依据事件证据"


def _lessons_context(snapshot: AgentEventSnapshot) -> dict[str, Any]:
    """把「本次请求真正知道的事」整理成经验检索上下文。

    内核不自己去数据库翻业务状态：调用方知道什么就传什么，不知道就不传。
    这样「本次引用了哪条经验」永远建立在**已知事实**上，
    而不是内核猜出来的 —— 评委追问"凭什么叫合并优先"时，
    能指着这个布尔值回答"因为这次快照里确实有可合并工单"。

    判定口径与工具链保持一致（同样用 dispatch_* 阈值筛候选），
    避免出现"经验说没候选、工具链却选出机器人"这种自相矛盾的观感。
    """
    candidates = [
        item
        for item in snapshot.candidates
        if item.battery >= settings.dispatch_min_battery
        and item.bin_usage < settings.dispatch_max_bin_usage
        and item.distance_m <= settings.dispatch_range_meters
    ]
    confidence = float(snapshot.event.max_confidence or 0)
    return {
        "merge_candidate": snapshot.merge_task_id is not None,
        "no_candidate": len(candidates) == 0,
        "high_priority": snapshot.event.main_class in WasteClass.HIGH_PRIORITY,
        "evidence_insufficient": (not snapshot.event.evidence_url)
        or confidence < ASSESS_CONFIDENCE_FLOOR,
        "township": str(snapshot.event.township or ""),
    }


def _policy_lookup(ctx: ToolContext) -> dict[str, Any]:
    """研判 Agent 第一步：把预取的政策依据交给轨迹（只读）。"""
    snapshot = _current_snapshot()
    if ctx.input.get("event_id") != snapshot.event.event_id:
        raise TaskConflictError("工具输入事件与请求快照不一致")
    return {
        "event_id": snapshot.event.event_id,
        "query": snapshot.policy_query,
        "policy_refs": snapshot.policy_refs,
        "policy_count": len(snapshot.policy_refs),
        "degraded": snapshot.policy_degraded,
        "note": snapshot.policy_note or "",
    }


def _analysis_assess(ctx: ToolContext) -> dict[str, Any]:
    """研判 Agent 第二步：综合事件证据与政策依据给出结论（只读、确定性）。

    ★ 这里没有模型参与，结论完全由可复现的规则得出：
      证据不足（无证据图或置信度低于下限）→ manual_review；
      近邻已有同类活跃工单 → merge_first；否则 dispatch。
      研判结论是真门禁：manual_review 会让期望校验直接终止 run。
    """
    snapshot = _current_snapshot()
    if ctx.input.get("event_id") != snapshot.event.event_id:
        raise TaskConflictError("工具输入事件与请求快照不一致")

    refs_input = ctx.input.get("policy_refs")
    refs: list[dict[str, Any]] = (
        [item for item in refs_input if isinstance(item, dict)]
        if isinstance(refs_input, list)
        else []
    )

    event = snapshot.event
    confidence = float(event.max_confidence or 0)
    det_count = int(event.det_count or 0)
    has_evidence = bool(event.evidence_url)
    high_priority = event.main_class in WasteClass.HIGH_PRIORITY

    basis: list[str] = [
        f"事件类型 {WasteClass.LABELS.get(event.main_class, event.main_class)}"
        f"（{event.main_class}），检出 {det_count} 处，最高置信度 {confidence:.2f}"
    ]
    if high_priority:
        basis.append("该类别属政策重点治理类别，需优先处置")
    if not has_evidence:
        basis.append("事件缺少证据图（evidence_url 为空），不足以支撑自动派单")
    elif confidence < ASSESS_CONFIDENCE_FLOOR:
        basis.append(
            f"识别置信度 {confidence:.2f} 低于自动派单下限 {ASSESS_CONFIDENCE_FLOOR:.2f}"
        )
    if refs:
        titles = "、".join(
            str(ref.get("title")) for ref in refs[:3] if ref.get("title")
        )
        basis.append(f"命中 {len(refs)} 条知识依据：{titles}")
    else:
        basis.append("未命中知识条目，本结论仅依据事件自身证据")
    if snapshot.merge_task_id:
        basis.append(
            f"近邻已存在同类活跃工单 {snapshot.merge_task_id}，"
            "按合并窗口应并入而非新建"
        )

    if not has_evidence or confidence < ASSESS_CONFIDENCE_FLOOR:
        recommended_action = "manual_review"
        risk_level = "high"
    elif snapshot.merge_task_id:
        recommended_action = "merge_first"
        risk_level = "normal"
    else:
        recommended_action = "dispatch"
        risk_level = "normal"

    return {
        "assessment_id": f"asm_{uuid.uuid4().hex[:12]}",
        "risk_level": risk_level,
        "recommended_action": recommended_action,
        "basis": basis,
        "confidence": round(confidence, 4),
        "policy_ref_count": len(refs),
        "degraded": snapshot.policy_degraded,
    }


WORK_ORDER_PLAN: list[dict[str, Any]] = [
    {
        "tool": "event.get",
        "input": {"event_id": "{event_id}"},
        "summary": "读取事件状态、类别与坐标",
        "expect": {
            "key": "dispatchable",
            "contains": (True,),
            "error_code": ErrorCode.TASK_CONFLICT,
        },
    },
    {
        "tool": "device.query_available",
        "input": {"event_id": "{event_id}"},
        "summary": "筛选在线、电量与仓容合格的候选机器人",
        "expect": {
            "key": "candidates",
            "min_items": 1,
            "error_code": ErrorCode.NO_ROBOT_AVAILABLE,
        },
    },
    {
        "tool": "dispatch.plan",
        "input": {"event_id": "{event_id}", "candidates": "{candidates}"},
        "summary": "按同类别顺路加权与距离选择机器人",
        "expect": {"key": "robot_id", "error_code": ErrorCode.NO_ROBOT_AVAILABLE},
    },
    {
        "tool": "task.create_or_merge",
        "input": {"event_id": "{event_id}", "robot_id": "{robot_id}"},
        "summary": "创建或合并真实清理工单",
        "expect": {"key": "task_id", "error_code": ErrorCode.TOOL_FAILED},
    },
]

# ----------------------------------------------------------------------
# 多角色协同（P1）—— 「事件研判 Agent」→「调度执行 Agent」
# ----------------------------------------------------------------------
# 设计纪律（为什么这样做）：
#   1. 现有 WORK_ORDER_PLAN 一个字符都不动 —— 它是评测基线（23 场景 / 14 指标）
#      跑出来的那套计划，改它等于把已有证据作废。多角色是**另一套计划**。
#   2. 角色只是计划项的元数据（RuleStep.role），由内核记进
#      runtime_state["step_roles"]；不改状态机、不改 t_agent_step 冻结表结构。
#   3. 研判不是表演：它有一个真门禁 —— 证据不足时判定 manual_review，
#      期望校验直接终止 run，事件保持 new、工单不落库。评委问
#      「研判 Agent 到底管什么用」，答案就是「它会拦住不该自动派单的单」。
ROLE_ASSESSOR = "事件研判 Agent"
ROLE_DISPATCHER = "调度执行 Agent"

# 计划变体键（前端 / API 用 'single' | 'team' 选择）
PLAN_SINGLE = "single"
PLAN_TEAM = "team"

# 置信度下限：低于它视为「证据不足」，研判不下自动派单结论。
ASSESS_CONFIDENCE_FLOOR = 0.5

EVENT_ASSESSMENT_PLAN: list[dict[str, Any]] = [
    {
        "role": ROLE_ASSESSOR,
        "tool": "policy.lookup",
        "input": {"event_id": "{event_id}"},
        "summary": "检索本辖区治理政策与处置口径，形成可引用依据",
        # 知识库不可用时该工具自行降级为「无政策依据」并照常返回，
        # 所以这里**不设**期望：研判能力不因知识库缺席而中断派单主链路。
        "on_error": "terminate",
    },
    {
        "role": ROLE_ASSESSOR,
        "tool": "analysis.assess",
        "input": {"event_id": "{event_id}", "policy_refs": "{policy_refs}"},
        "summary": "综合事件证据与政策依据给出研判结论与处置建议",
        "expect": {
            "key": "recommended_action",
            "contains": ("dispatch", "merge_first"),
            # manual_review（证据不足）→ 安全终止；错误码落在冻结清单内。
            "error_code": ErrorCode.POLICY_DENIED,
        },
    },
]

# 调度执行 Agent：复用既有四步，只补一个角色标记。
WORK_ORDER_PLAN_WITH_ROLE: list[dict[str, Any]] = [
    {**step, "role": ROLE_DISPATCHER} for step in WORK_ORDER_PLAN
]

TEAM_RUN_PLAN: list[dict[str, Any]] = EVENT_ASSESSMENT_PLAN + WORK_ORDER_PLAN_WITH_ROLE

RULE_PLANS: dict[str, list[dict[str, Any]]] = {
    PLAN_SINGLE: WORK_ORDER_PLAN,
    PLAN_TEAM: TEAM_RUN_PLAN,
}


def _tool_definitions() -> list[ToolDefinition]:
    """定义 Agent 可调用工具；所有结果都是请求快照，关闭跨请求缓存。"""
    return [
        ToolDefinition(
            name="event.get",
            version="1.0.0",
            description="读取单个事件的业务快照",
            input_schema={
                "type": "object",
                "required": ["event_id"],
                "properties": {"event_id": {"type": "string"}},
            },
            output_schema={
                "type": "object",
                "required": ["event_id", "status", "dispatchable"],
                "properties": {
                    "event_id": {"type": "string"},
                    "status": {"type": "string"},
                    "dispatchable": {"type": "boolean"},
                },
            },
            risk_level=RiskLevel.READ_ONLY,
            timeout_ms=1000,
            idempotent=False,
            allowed_roles=("operator", "admin"),
            handler=_event_get,
        ),
        ToolDefinition(
            name="device.query_available",
            version="1.0.0",
            description="筛选可用机器人候选",
            input_schema={
                "type": "object",
                "required": ["event_id"],
                "properties": {"event_id": {"type": "string"}},
            },
            output_schema={
                "type": "object",
                "required": ["candidates"],
                "properties": {"candidates": {"type": "array"}},
            },
            risk_level=RiskLevel.READ_ONLY,
            timeout_ms=1000,
            idempotent=False,
            allowed_roles=("operator", "admin"),
            handler=_device_query_available,
        ),
        ToolDefinition(
            name="dispatch.plan",
            version="1.0.0",
            description="生成确定性的机器人派单方案",
            input_schema={
                "type": "object",
                "required": ["event_id", "candidates"],
                "properties": {
                    "event_id": {"type": "string"},
                    "candidates": {"type": "array"},
                },
            },
            output_schema={
                "type": "object",
                "required": ["robot_id", "distance_m", "reason"],
                "properties": {
                    "robot_id": {"type": "string"},
                    "distance_m": {"type": "number"},
                    "reason": {"type": "string"},
                },
            },
            risk_level=RiskLevel.READ_ONLY,
            timeout_ms=1000,
            idempotent=False,
            allowed_roles=("operator", "admin"),
            handler=_dispatch_plan,
        ),
        ToolDefinition(
            name="task.create_or_merge",
            version="1.0.0",
            description="创建真实清理工单，或复用附近可合并工单",
            input_schema={
                "type": "object",
                "required": ["event_id", "robot_id"],
                "properties": {
                    "event_id": {"type": "string"},
                    "robot_id": {"type": "string"},
                },
            },
            output_schema={
                "type": "object",
                "required": ["task_id", "action", "created"],
                "properties": {
                    "task_id": {"type": "string"},
                    "action": {"type": "string"},
                    "created": {"type": "boolean"},
                },
            },
            risk_level=RiskLevel.WRITE,
            timeout_ms=1000,
            idempotent=False,
            allowed_roles=("operator", "admin"),
            handler=_task_create_or_merge,
        ),
        # ---- 研判 Agent 工具（多角色计划专用；单角色计划不会调用） ----
        ToolDefinition(
            name="policy.lookup",
            version="1.0.0",
            description="检索治理政策与处置口径，形成可引用的研判依据",
            input_schema={
                "type": "object",
                "required": ["event_id"],
                "properties": {"event_id": {"type": "string"}},
            },
            output_schema={
                "type": "object",
                "required": ["event_id", "query", "policy_refs", "policy_count"],
                "properties": {
                    "event_id": {"type": "string"},
                    "query": {"type": "string"},
                    "policy_refs": {"type": "array"},
                    "policy_count": {"type": "integer"},
                    # degraded / note 允许为空值，故不声明 type（校验器按 JSON Schema
                    # 语义忽略未声明类型的属性，同时仍要求上面四个字段存在）。
                    "degraded": {},
                    "note": {},
                },
            },
            risk_level=RiskLevel.READ_ONLY,
            timeout_ms=1000,
            idempotent=False,
            allowed_roles=("operator", "admin"),
            handler=_policy_lookup,
        ),
        ToolDefinition(
            name="analysis.assess",
            version="1.0.0",
            description="依据事件证据与政策依据给出确定性研判结论（含门禁建议）",
            input_schema={
                "type": "object",
                "required": ["event_id", "policy_refs"],
                "properties": {
                    "event_id": {"type": "string"},
                    "policy_refs": {"type": "array"},
                },
            },
            output_schema={
                "type": "object",
                "required": [
                    "assessment_id",
                    "risk_level",
                    "recommended_action",
                    "basis",
                    "confidence",
                ],
                "properties": {
                    "assessment_id": {"type": "string"},
                    "risk_level": {"type": "string"},
                    "recommended_action": {"type": "string"},
                    "basis": {"type": "array"},
                    "confidence": {"type": "number"},
                    "policy_ref_count": {"type": "integer"},
                    "degraded": {},
                },
            },
            risk_level=RiskLevel.READ_ONLY,
            timeout_ms=1000,
            idempotent=False,
            allowed_roles=("operator", "admin"),
            handler=_analysis_assess,
        ),
    ]


def _build_persistent_repositories() -> tuple[Any, Any]:
    """按配置构造 SQL 持久化仓储（WP-10，默认关闭）。

    同步 Engine 只在明确开启 `agent_persistent_repository_enabled` 时创建；
    数据库不可用/驱动缺失时抛异常，由调用方回退内存仓储，
    保证「内存默认、数据库可选」—— 离线测试与演示不受影响。
    """
    from sqlalchemy import create_engine

    engine = create_engine(settings.database_url_sync, pool_pre_ping=True)
    return create_repositories(engine)


def _build_runtime() -> AgentRuntime:
    registry = ToolRegistry()
    for tool in _tool_definitions():
        registry.register(tool)
    # WP-10：默认内存仓储；仅当配置开启时换 SQL 仓储，失败回退内存并告警
    run_repository: Any = None
    approval_repository: Any = None
    if settings.agent_persistent_repository_enabled:
        try:
            run_repository, approval_repository = _build_persistent_repositories()
        except Exception as exc:   # noqa: BLE001
            logger.warning(f"[Agent] 持久化仓储初始化失败，回退内存仓储：{exc}")
    approval_risks = (
        (RiskLevel.DEVICE_COMMAND, RiskLevel.SENSITIVE)
        if not settings.agent_require_approval_for_write
        else (RiskLevel.WRITE, RiskLevel.DEVICE_COMMAND, RiskLevel.SENSITIVE)
    )
    runtime_config = RuntimeConfig(
        policy_version="rules-work-order-v1.0",
        rule_plan=WORK_ORDER_PLAN,
        # 计划变体表：请求用 plan_key 选（single / team）。
        # 单角色仍是 rule_plan 的默认值，既有行为逐字节不变。
        rule_plans=RULE_PLANS,
        # 跨 run 经验闭环：本进程内累积（MemoryStore 目前只有内存实现，
        # 重启清零 —— 不假装它持久化）。评测基线场景自带 RuntimeConfig，
        # 不受这里影响。
        lessons_enabled=True,
        model_available=False,
        require_approval_risk=approval_risks,
    )
    model_adapter: ModelAdapterPlanner | None = None
    if settings.agent_model_adapter_enabled:
        missing = [
            name
            for name, value in (
                ("AGENT_MODEL_BASE_URL", settings.agent_model_base_url),
                ("AGENT_MODEL_NAME", settings.agent_model_name),
            )
            if not str(value or "").strip()
        ]
        model_client: OpenAICompatibleModelClient | None = None
        if missing:
            logger.warning(
                "[Agent] 模型适配层已启用但配置不完整，将以规则模式运行；"
                f"缺少 {', '.join(missing)}"
            )
        else:
            model_client = OpenAICompatibleModelClient(
                base_url=settings.agent_model_base_url,
                model=settings.agent_model_name,
                api_key=settings.agent_model_api_key,
                timeout_ms=settings.agent_model_adapter_timeout_ms,
                max_output_tokens=settings.agent_model_max_output_tokens,
            )
        model_adapter = ModelAdapterPlanner(
            client=model_client,
            enabled=True,
            config=runtime_config,
            timeout_ms=settings.agent_model_adapter_timeout_ms,
            max_steps=settings.agent_model_adapter_max_steps,
        )
    return AgentRuntime(
        tool_registry=registry,
        runtime_config=runtime_config,
        run_repository=run_repository,
        approval_repository=approval_repository,
        model_adapter=model_adapter,
    )


_RUNTIME: AgentRuntime | None = None
_RUN_TASK_RESULTS: dict[str, dict[str, Any]] = {}


def get_agent_runtime() -> AgentRuntime:
    """返回进程内 Agent Runtime（单例、懒加载）。

    未初始化 / 无运行数据时 `status().state` 为 `idle`（真实状态，
    不写死 running）；构造失败属内部错误，由调用方兜底为 unavailable。
    """
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = _build_runtime()
    return _RUNTIME


def reset_agent_runtime_for_tests() -> None:
    """测试隔离入口：清空运行时与请求级产物。"""
    global _RUNTIME
    _RUNTIME = None
    _RUN_TASK_RESULTS.clear()


# ----------------------------------------------------------------------
# ORM 镜像与输出映射
# ----------------------------------------------------------------------


def _runtime_status_out(runtime: AgentRuntime) -> AgentRuntimeStatusOut:
    status = runtime.status()
    # 审批策略取自内核实际生效的配置（RuntimeConfig.require_approval_risk），
    # 不是再读一遍 settings —— 演示前自检要看到的是「内核真的会拦」，
    # 而不是「环境变量写没写对」。
    approval_risks = [
        risk.value if isinstance(risk, RiskLevel) else str(risk)
        for risk in runtime.config.require_approval_risk
    ]
    return AgentRuntimeStatusOut(
        state=status.state,
        model_available=status.model_available,
        rule_mode=status.rule_mode,
        policy_version=status.policy_version,
        active_runs=status.active_runs,
        total_runs=status.total_runs,
        pending_approvals=status.pending_approvals,
        tools_registered=status.tools_registered,
        uptime_ms=status.uptime_ms,
        persistence="mirrored",
        runtime="in_process",
        require_approval_for_write=RiskLevel.WRITE in runtime.config.require_approval_risk,
        approval_risk_levels=approval_risks,
        plan_variants=sorted((runtime.config.rule_plans or {}).keys()),
        lessons=[
            AgentLessonOut(
                lesson_id=lesson.lesson_id,
                scope_id=lesson.scope_id,
                kind=lesson.kind,
                condition=lesson.condition,
                guidance=lesson.guidance,
                confidence=lesson.confidence,
                hit_count=lesson.hit_count,
                confirm_count=lesson.confirm_count,
                source_run_id=lesson.source_run_id,
                updated_at=lesson.updated_at,
            )
            for lesson in runtime.lessons.all_lessons()
        ],
    )


def _step_roles(run: Any) -> dict[str, str]:
    """读出 run 状态快照里的「step_no → 角色」映射（多角色计划才有）。"""
    roles = run.runtime_state.get("step_roles") if run is not None else None
    return roles if isinstance(roles, dict) else {}


def _role_list_from_map(roles: dict[Any, Any]) -> list[str]:
    """按 step_no 顺序去重列出参与过的角色（内存快照与 ORM 回放共用）。"""
    ordered: list[str] = []
    for key in sorted(
        roles,
        key=lambda item: int(item) if str(item).isdigit() else 0,
    ):
        role = roles[key]
        if role and str(role) not in ordered:
            ordered.append(str(role))
    return ordered


def _run_role_list(run: Any) -> list[str]:
    """按 step_no 顺序去重列出本次运行参与过的角色。"""
    return _role_list_from_map(_step_roles(run))


def _decision_out(bindings: Any) -> AgentDecisionOut | None:
    """从 run 的绑定变量还原派单决策快照（大屏地图联动用）。

    没有绑定变量、或还没走到 dispatch.plan 时返回 None —— 不拼"半张快照"：
    前端据此能区分"这次没决策可标注"与"决策就是空的"。
    """
    if not isinstance(bindings, dict):
        return None
    if "robot_id" not in bindings and "candidates" not in bindings:
        return None

    raw_candidates = bindings.get("candidates")
    raw_basis = bindings.get("basis")
    raw_distance = bindings.get("distance_m")
    try:
        distance_value = float(raw_distance) if raw_distance is not None else None
    except (TypeError, ValueError):
        distance_value = None

    def _text(key: str) -> str | None:
        value = bindings.get(key)
        return str(value) if value not in (None, "") else None

    return AgentDecisionOut(
        candidates=(
            [item for item in raw_candidates if isinstance(item, dict)]
            if isinstance(raw_candidates, list)
            else []
        ),
        selected_robot_id=_text("robot_id"),
        selected_robot_name=_text("robot_name"),
        distance_m=distance_value,
        reason=_text("reason"),
        risk_level=_text("risk_level"),
        recommended_action=_text("recommended_action"),
        basis=(
            [str(item) for item in raw_basis] if isinstance(raw_basis, list) else []
        ),
    )


def _step_out(step: AgentStep, roles: dict[str, Any] | None = None) -> AgentStepOut:
    role = (roles or {}).get(str(step.step_no))
    return AgentStepOut(
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
        latency_ms=step.latency_ms or 0,
        error_code=step.error_code,
        created_at=step.created_at,
        role=str(role) if role else None,
    )


def _run_out(
    runtime: AgentRuntime,
    run_id: str,
    *,
    include_steps: bool = True,
    idempotent_replay: bool = False,
) -> AgentRunOut:
    run = runtime.runs.get(run_id)
    if run is None:
        raise AppException(
            code=AGENT_RUN_NOT_FOUND,
            message=f"Agent 运行 {run_id} 不存在",
            http_status=200,
        )
    steps = runtime.runs.steps(run_id) if include_steps else []
    roles = _step_roles(run)
    task_result = run.runtime_state.get("task_result")
    if not isinstance(task_result, dict):
        task_result = _RUN_TASK_RESULTS.get(run_id, {})
    return AgentRunOut(
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
        error_code=(
            run.termination_reason
            if run.status in (AgentRunStatus.FAILED, AgentRunStatus.EXPIRED)
            else None
        ),
        idempotent_replay=idempotent_replay,
        pending_approval_ids=[
            approval_id
            for approval_id in run.runtime_state.get("approvals") or []
            if (record := runtime.approvals.get(approval_id)) is not None
            and record.decision is None
        ],
        steps=[_step_out(step, roles) for step in steps],
        task_id=task_result.get("task_id"),
        task_action=task_result.get("action"),
        plan_key=getattr(run.request, "plan_key", None) or PLAN_SINGLE,
        roles=_run_role_list(run),
        decision=_decision_out(run.runtime_state.get("bindings")),
        lesson_hits=[
            str(item) for item in (run.runtime_state.get("lesson_hits") or [])
        ],
    )


def _run_out_from_orm(
    run: AgentRunORM,
    steps: list[AgentStepORM],
    state: AgentRunStateORM | None = None,
) -> AgentRunOut:
    task_result: dict[str, Any] = {}
    roles: dict[str, Any] = {}
    bindings: Any = None
    lesson_hits: list[Any] = []
    if state is not None and state.runtime_state_json:
        try:
            runtime_state = json.loads(state.runtime_state_json)
        except (TypeError, ValueError):
            runtime_state = {}
        if isinstance(runtime_state, dict):
            candidate = runtime_state.get("task_result")
            if isinstance(candidate, dict):
                task_result = candidate
            # 重启后只读回放同样要带上角色归属、决策快照与经验引用
            # （step_roles / bindings / lesson_hits 都随状态快照落库）。
            stored_roles = runtime_state.get("step_roles")
            if isinstance(stored_roles, dict):
                roles = stored_roles
            bindings = runtime_state.get("bindings")
            stored_hits = runtime_state.get("lesson_hits")
            if isinstance(stored_hits, list):
                lesson_hits = stored_hits
    return AgentRunOut(
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
        error_code=(
            run.termination_reason
            if run.status in (AgentRunStatus.FAILED, AgentRunStatus.EXPIRED)
            else None
        ),
        task_id=task_result.get("task_id"),
        task_action=task_result.get("action"),
        roles=_role_list_from_map(roles),
        decision=_decision_out(bindings),
        lesson_hits=[str(item) for item in lesson_hits],
        steps=[
            AgentStepOut(
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
                latency_ms=step.latency_ms or 0,
                error_code=step.error_code,
                created_at=step.created_at,
                role=(
                    str(roles[str(step.step_no)])
                    if str(step.step_no) in roles
                    else None
                ),
            )
            for step in steps
        ],
    )


def _approval_out(record: Any) -> AgentApprovalOut:
    return AgentApprovalOut(
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


def _created_at_sort_key(value: datetime | None) -> datetime:
    """把运行创建时间统一成 aware UTC，避免 None/naive/aware 混排报错。"""
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _mirror_run_and_steps(
    session: AsyncSession,
    runtime: AgentRuntime,
    run_id: str,
) -> None:
    """把当前运行镜像到 ORM，供审计查询与服务重启后只读回放。"""
    run = runtime.runs.get(run_id)
    if run is None:
        return

    existing = (
        await session.execute(select(AgentRunORM).where(AgentRunORM.run_id == run_id))
    ).scalar_one_or_none()
    if existing is None:
        existing = AgentRunORM(
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
        session.add(existing)
    else:
        existing.status = run.status
        existing.policy_version = run.policy_version
        existing.finished_at = run.finished_at
        existing.termination_reason = run.termination_reason
        existing.updated_at = run.updated_at

    persisted_step_ids = set(
        (
            await session.execute(
                select(AgentStepORM.step_id).where(AgentStepORM.run_id == run_id)
            )
        )
        .scalars()
        .all()
    )
    for step in runtime.runs.steps(run_id):
        if step.step_id in persisted_step_ids:
            continue
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
    await session.flush()


async def _mirror_approval(session: AsyncSession, record: Any) -> None:
    existing = (
        await session.execute(
            select(AgentApprovalORM).where(
                AgentApprovalORM.approval_id == record.approval_id
            )
        )
    ).scalar_one_or_none()
    if existing is None:
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
                requested_at=record.requested_at or datetime.now(timezone.utc),
                decided_at=record.decided_at,
            )
        )
    else:
        existing.decided_by = record.decided_by
        existing.decision = record.decision
        existing.reason = record.reason
        existing.decided_at = record.decided_at
    await session.flush()


async def _mirror_runtime_result(
    session: AsyncSession,
    runtime: AgentRuntime,
    run_id: str,
) -> None:
    await _mirror_run_and_steps(session, runtime, run_id)
    run = runtime.runs.get(run_id)
    if run is None:
        return
    for approval_id in run.runtime_state.get("approvals") or []:
        record = runtime.approvals.get(approval_id)
        if record is not None:
            await _mirror_approval(session, record)


# ----------------------------------------------------------------------
# 运行辅助
# ----------------------------------------------------------------------


def _make_run_request(
    payload: AgentRunCreate,
    user: CurrentUser,
) -> AgentRunRequest:
    # plan_key=None（即 mode 未指定）表示用 RuntimeConfig.rule_plan，
    # 也就是既有单角色派单管道 —— 默认路径连字符串比较都不引入。
    plan_key = payload.mode if payload.mode == PLAN_TEAM else None
    if plan_key == PLAN_TEAM:
        default_objective = (
            f"处置事件 {payload.event_id}：检索研判依据 → 输出研判结论 → "
            "读取事件 → 筛选机器人 → 生成方案 → 创建工单"
        )
    else:
        default_objective = (
            f"处置事件 {payload.event_id}：读取事件 → 筛选机器人 → 生成方案 → 创建工单"
        )
    return AgentRunRequest(
        trigger_type="event",
        objective=payload.objective or default_objective,
        actor=user.username,
        role=user.role,
        params={"event_id": payload.event_id},
        idempotency_key=payload.idempotency_key,
        plan_key=plan_key,
    )


async def _commit_successful_task(
    session: AsyncSession,
    snapshot: AgentEventSnapshot,
    runtime: AgentRuntime,
    run_id: str,
    user: CurrentUser,
) -> None:
    def _record_task_result(task_id: str, action: str) -> None:
        result = {"task_id": task_id, "action": action}
        _RUN_TASK_RESULTS[run_id] = result
        run = runtime.runs.get(run_id)
        if run is not None:
            run.runtime_state["task_result"] = dict(result)
            runtime.runs.save(run)

    task = snapshot.prepared_task
    if task is None:
        if snapshot.merge_task_id is None:
            return
        snapshot.event.status = EventStatus.DISPATCHED
        await session.flush()
        _record_task_result(snapshot.merge_task_id, "merged")
        await record_audit(
            session,
            username=user.username,
            role=user.role,
            action="agent_task_merge",
            target_type="task",
            target_id=snapshot.merge_task_id,
            detail=f"run={run_id}; event={snapshot.event.event_id}; action=merged",
        )
        return

    session.add(task)
    snapshot.event.status = EventStatus.DISPATCHED

    if snapshot.selected_robot_id:
        robot = (
            await session.execute(
                select(Device).where(Device.device_id == snapshot.selected_robot_id)
            )
        ).scalar_one_or_none()
        if robot is not None:
            meta = dict(robot.meta or {})
            meta["current_task_id"] = task.task_id
            robot.meta = meta

    await session.flush()
    _record_task_result(task.task_id, "created")
    await record_audit(
        session,
        username=user.username,
        role=user.role,
        action="agent_task_create",
        target_type="task",
        target_id=task.task_id,
        detail=f"run={run_id}; event={snapshot.event.event_id}; robot={snapshot.selected_robot_id}",
    )
    await finalize_dispatch(task)


async def _record_team_decision_trace(
    session: AsyncSession,
    runtime: AgentRuntime,
    run_id: str,
    user: CurrentUser,
) -> None:
    """把研判结论与政策依据写成一条知识决策链（DecisionTrace）。

    为什么要有这一步：多角色协同的价值不能只停在轨迹的角标上。
    研判 Agent 读到的每一条政策依据，都要以「决策链 + 证据链」的形式
    落进知识模块的决策列表（KnowledgeView 决策页签），
    这样「这个单为什么派、依据是哪份政策第几条」是可点开核对的，
    而不是只在大屏上闪一句总结。

    边界纪律：
    - 只在 team 计划下写；单角色 run 不产生决策链，不污染决策列表；
    - 失败只告警不抛出 —— 决策留痕是增强项，绝不能把已经成功的派单回滚。
    """
    run = runtime.runs.get(run_id)
    if run is None or getattr(run.request, "plan_key", None) != PLAN_TEAM:
        return
    bindings = run.runtime_state.get("bindings")
    if not isinstance(bindings, dict):
        return
    assessment_id = bindings.get("assessment_id")
    if not assessment_id:
        # 研判没跑出结论（例如更早就重规划了）时不留空壳决策链
        return

    evidence: list[DecisionEvidenceIn] = []
    refs = bindings.get("policy_refs")
    if isinstance(refs, list):
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            citation = str(ref.get("citation") or ref.get("title") or "").strip()
            if not citation:
                continue
            evidence.append(
                DecisionEvidenceIn(
                    asset_id=ref.get("asset_id"),
                    asset_version_id=ref.get("asset_version_id"),
                    hop_no=int(ref.get("hop_no") or 0),
                    citation_text=citation[:4000],
                    source_uri=ref.get("source_uri"),
                    score=float(ref.get("score") or 0.0),
                    metadata={
                        "matched_node_ids": ref.get("matched_node_ids") or [],
                        "matched_relation_ids": ref.get("matched_relation_ids") or [],
                    },
                )
            )

    basis = bindings.get("basis")
    basis_text = "；".join(str(item) for item in basis) if isinstance(basis, list) else ""
    answer_summary = (
        f"研判结论：{bindings.get('recommended_action')}"
        f"（风险等级 {bindings.get('risk_level')}，"
        f"证据置信度 {bindings.get('confidence')}）。{basis_text}"
    )
    event_id = str((run.request.params or {}).get("event_id") or "")

    try:
        trace = await KnowledgeService(session).create_decision(
            DecisionCreate(
                question=f"事件 {event_id} 是否应自动派单？依据是什么？",
                answer_summary=answer_summary[:8000],
                run_id=run_id,
                policy_version=run.policy_version,
                hop_depth=2,
                evidence=evidence,
                metadata={
                    "source": "agent_run",
                    "plan_key": PLAN_TEAM,
                    "assessment_id": str(assessment_id),
                    "policy_degraded": bool(bindings.get("degraded")),
                },
            ),
            username=user.username,
            # 决策链本身不按辖区过滤（DecisionTrace 无 township 列，列表接口
            # 也从不做辖区过滤）；这里显式传 None 是为了让知识库里
            # township 为空的县级政策文件也能作为证据被引用。
            township_scope=None,
        )
    except Exception as exc:  # noqa: BLE001 —— 留痕失败不得影响已完成的派单
        logger.warning(f"[Agent] 决策链留痕失败（不影响派单结果）：{exc}")
        return

    await record_audit(
        session,
        username=user.username,
        role=user.role,
        action="agent_decision_trace",
        target_type="decision_trace",
        target_id=trace.trace_id,
        detail=(
            f"run={run_id}; evidence={len(evidence)}; "
            f"action={bindings.get('recommended_action')}"
        ),
    )


async def _load_persisted_run(
    session: AsyncSession,
    run_id: str,
) -> tuple[AgentRunORM, list[AgentStepORM], AgentRunStateORM | None] | None:
    run = (
        await session.execute(select(AgentRunORM).where(AgentRunORM.run_id == run_id))
    ).scalar_one_or_none()
    if run is None:
        return None
    steps = list(
        (
            await session.execute(
                select(AgentStepORM)
                .where(AgentStepORM.run_id == run_id)
                .order_by(AgentStepORM.step_no)
            )
        )
        .scalars()
        .all()
    )
    state = (
        await session.execute(
            select(AgentRunStateORM).where(AgentRunStateORM.run_id == run_id)
        )
    ).scalar_one_or_none()
    return run, steps, state


# ----------------------------------------------------------------------
# 冻结 API（手册 3.8：前缀 /api/v1/agents）
# ----------------------------------------------------------------------


@router.get(
    "/runtime/status",
    response_model=ApiResponse[AgentRuntimeStatusOut],
    summary="Agent 运行时状态",
)
async def get_runtime_status(
    _user: CurrentUser = Depends(get_current_user),
):
    """真实运行时快照：无活跃 run → state=idle（不写死 running）。"""
    runtime = get_agent_runtime()
    return ApiResponse.ok(_runtime_status_out(runtime))


@router.get(
    "/runs",
    response_model=ApiResponse[AgentRunPageOut],
    summary="Agent 运行列表（分页，按创建时间倒序）",
)
async def list_runs(
    status: str | None = Query(default=None, description="按运行状态过滤"),
    page: int = Query(default=1, ge=1, description="页码（从 1 开始）"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
):
    """按创建时间倒序分页返回运行列表（PageResult 风格）。

    内存运行时（WP-01 仓储）与 ORM 回放镜像（WP-02）按 run_id 去重合并；
    分页与总数在合并结果上计算，保证「重启后只读回放」也能列出历史 run。
    """
    runtime = get_agent_runtime()
    memory_runs = runtime.runs.list(status=status)  # 内核按 created_at 升序返回
    memory_ids = {run.run_id for run in memory_runs}

    stmt = (
        select(AgentRunORM, AgentRunStateORM)
        .outerjoin(AgentRunStateORM, AgentRunStateORM.run_id == AgentRunORM.run_id)
        .order_by(AgentRunORM.created_at.desc())
    )
    if status:
        stmt = stmt.where(AgentRunORM.status == status)
    persisted = [
        (run, state)
        for run, state in (await session.execute(stmt)).all()
        if run.run_id not in memory_ids
    ]

    combined: list[AgentRunOut] = [
        _run_out(runtime, run.run_id, include_steps=False) for run in memory_runs
    ]
    combined.extend(_run_out_from_orm(run, [], state) for run, state in persisted)
    # 统一按创建时间倒序（None 视为最早）
    combined.sort(key=lambda out: _created_at_sort_key(out.created_at), reverse=True)

    total = len(combined)
    start = (page - 1) * page_size
    return ApiResponse.ok(
        AgentRunPageOut(
            items=combined[start : start + page_size],
            meta=PageMeta(total=total, page=page, page_size=page_size),
        )
    )


@router.get(
    "/runs/{run_id}",
    response_model=ApiResponse[AgentRunOut],
    summary="Agent 运行详情",
)
async def get_run(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
):
    """返回真实 status（含 waiting_approval 等非终态）；无该 run → 6001。"""
    runtime = get_agent_runtime()
    if runtime.runs.get(run_id) is not None:
        return ApiResponse.ok(_run_out(runtime, run_id))

    persisted = await _load_persisted_run(session, run_id)
    if persisted is None:
        raise AppException(
            code=AGENT_RUN_NOT_FOUND,
            message=f"Agent 运行 {run_id} 不存在",
            http_status=200,
        )
    return ApiResponse.ok(_run_out_from_orm(*persisted))


@router.get(
    "/runs/{run_id}/steps",
    response_model=ApiResponse[list[AgentStepOut]],
    summary="Agent 运行轨迹",
)
async def list_run_steps(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
):
    runtime = get_agent_runtime()
    if runtime.runs.get(run_id) is not None:
        run = runtime.runs.get(run_id)
        roles = _step_roles(run)
        return ApiResponse.ok(
            [_step_out(step, roles) for step in runtime.runs.steps(run_id)]
        )

    persisted = await _load_persisted_run(session, run_id)
    if persisted is None:
        raise AppException(
            code=AGENT_RUN_NOT_FOUND,
            message=f"Agent 运行 {run_id} 不存在",
            http_status=200,
        )
    return ApiResponse.ok(_run_out_from_orm(*persisted).steps)


@router.post(
    "/runs",
    response_model=ApiResponse[AgentRunOut],
    summary="启动事件处置 Agent",
)
async def start_run(
    payload: AgentRunCreate,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_operator),
):
    runtime = get_agent_runtime()
    run_request = _make_run_request(payload, user)

    # 幂等重放：同一幂等键直接返回已有 run（replay 标记），不重新读取事件，
    # 避免首次运行已将事件标记 dispatched 后，重放反而被误判为冲突。
    if payload.idempotency_key:
        existing = runtime.runs.find_by_idempotency_key(payload.idempotency_key)
        if existing is not None:
            return ApiResponse.ok(
                _run_out(runtime, existing.run_id, idempotent_replay=True),
                message="幂等重放：返回已有运行",
            )

    snapshot = await _load_agent_snapshot(session, payload.event_id)
    if (payload.mode or PLAN_SINGLE) == PLAN_TEAM:
        # 研判依据是异步知识检索，必须在这里预取进快照；
        # 只有多角色计划会走这条路，单角色基线不多一次查询。
        await _prefetch_policy_refs(session, snapshot)
    # 把「本次请求真正知道的业务事实」交给内核，供跨 run 经验按条件检索。
    # 放在冲突判定之前：即便这次不派单，事实也应当被记录进请求上下文。
    run_request.params["main_class"] = str(snapshot.event.main_class or "")
    run_request.lessons_context.update(_lessons_context(snapshot))
    if snapshot.conflict_reason:
        raise AppException(
            code=AGENT_EVENT_NOT_DISPATCHABLE
            if snapshot.event.status != EventStatus.NEW
            else AGENT_TASK_CONFLICT,
            message=snapshot.conflict_reason,
            http_status=200,
        )

    token = _SNAPSHOT.set(snapshot)
    try:
        result = runtime.run(run_request)
        await _mirror_runtime_result(session, runtime, result.run_id)
        if result.status == AgentRunStatus.SUCCEEDED:
            await _commit_successful_task(
                session,
                snapshot,
                runtime,
                result.run_id,
                user,
            )
            await _record_team_decision_trace(session, runtime, result.run_id, user)
    finally:
        _SNAPSHOT.reset(token)

    await record_audit(
        session,
        username=user.username,
        role=user.role,
        action="agent_run_start",
        target_type="agent_run",
        target_id=result.run_id,
        detail=f"event={payload.event_id}; status={result.status}",
    )
    return ApiResponse.ok(_run_out(runtime, result.run_id))


@router.post(
    "/runs/{run_id}/cancel",
    response_model=ApiResponse[AgentRunOut],
    summary="取消 Agent 运行",
)
async def cancel_run(
    run_id: str,
    payload: AgentRunCancel,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_operator),
):
    runtime = get_agent_runtime()
    run = runtime.runs.get(run_id)
    if run is None:
        raise AppException(
            code=AGENT_RUN_NOT_FOUND,
            message=f"Agent 运行 {run_id} 不存在",
            http_status=200,
        )
    # 终态 run 不可取消（非运行中取消属非法操作）→ 6004
    if run.status in TERMINAL_STATUSES:
        raise AppException(
            code=AGENT_RUN_NOT_CANCELLABLE,
            message=f"run {run_id} 已进入终态（{run.status}），不可取消",
            http_status=200,
        )

    result = runtime.cancel(run_id, user.username, payload.reason)
    await _mirror_runtime_result(session, runtime, result.run_id)
    await record_audit(
        session,
        username=user.username,
        role=user.role,
        action="agent_run_cancel",
        target_type="agent_run",
        target_id=run_id,
        detail=payload.reason,
    )
    return ApiResponse.ok(_run_out(runtime, run_id), message="运行已取消")


@router.get(
    "/tools",
    response_model=ApiResponse[list[AgentToolOut]],
    summary="Agent 工具目录",
)
async def list_tools(_user: CurrentUser = Depends(get_current_user)):
    runtime = get_agent_runtime()
    return ApiResponse.ok(
        [
            AgentToolOut(
                name=tool.name,
                version=tool.version,
                description=tool.description,
                risk_level=(
                    tool.risk_level.value
                    if isinstance(tool.risk_level, RiskLevel)
                    else str(tool.risk_level)
                ),
                timeout_ms=tool.timeout_ms,
                idempotent=tool.idempotent,
                allowed_roles=list(tool.allowed_roles),
                input_schema=tool.input_schema,
                output_schema=tool.output_schema,
            )
            for tool in runtime.registry.list()
        ]
    )


@router.get(
    "/approvals",
    response_model=ApiResponse[list[AgentApprovalOut]],
    summary="人工审批列表",
)
async def list_approvals(_user: CurrentUser = Depends(get_current_user)):
    runtime = get_agent_runtime()
    records = runtime.approvals.list_all()
    return ApiResponse.ok([_approval_out(record) for record in records])


@router.post(
    "/approvals/{approval_id}/decide",
    response_model=ApiResponse[AgentRunOut],
    summary="审批高风险 Agent 动作",
)
async def decide_approval(
    approval_id: str,
    payload: AgentApprovalDecision,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """审批权限守卫（admin/approver）→ 6003/6001/6002 → decide → resume。

    决定后 run 状态按 WP-01 语义推进（waiting_approval → executing → 终态）。
    """
    _require_approver(user)
    runtime = get_agent_runtime()
    record = runtime.approvals.get(approval_id)
    if record is None:
        raise AppException(
            code=AGENT_APPROVAL_NOT_DECIDABLE,
            message=f"审批 {approval_id} 不存在",
            http_status=200,
        )
    if record.decision is not None:
        raise AppException(
            code=AGENT_APPROVAL_NOT_DECIDABLE,
            message=f"审批 {approval_id} 已决策（{record.decision}），不可重复决定",
            http_status=200,
        )

    run = runtime.runs.get(record.run_id)
    if run is None:
        raise AppException(
            code=AGENT_RUN_NOT_FOUND,
            message=f"审批关联运行 {record.run_id} 不存在",
            http_status=200,
        )
    # 状态一致性守卫：审批仍挂起但 run 已终态 → 非法状态迁移，拒绝推进
    if run.status in TERMINAL_STATUSES:
        raise AppException(
            code=AGENT_ILLEGAL_STATE_TRANSITION,
            message=f"run {record.run_id} 已进入终态（{run.status}），不可从审批继续推进",
            http_status=200,
        )

    record = runtime.approvals.decide(
        approval_id,
        user.username,
        payload.decision,
        payload.reason,
    )
    await _mirror_approval(session, record)

    event_id = str((run.request.params or {}).get("event_id") or "")
    snapshot = await _load_agent_snapshot(session, event_id)
    if getattr(run.request, "plan_key", None) == PLAN_TEAM:
        # 审批续跑会从 plan_idx=0 重新进入执行阶段，研判两步也在其中，
        # 所以这里必须用同一套快照重放研判依据，否则续跑后的研判是空依据。
        await _prefetch_policy_refs(session, snapshot)
    token = _SNAPSHOT.set(snapshot)
    try:
        result = runtime.resume(record.run_id)
        await _mirror_runtime_result(session, runtime, result.run_id)
        if result.status == AgentRunStatus.SUCCEEDED:
            await _commit_successful_task(
                session,
                snapshot,
                runtime,
                result.run_id,
                user,
            )
            await _record_team_decision_trace(session, runtime, result.run_id, user)
    finally:
        _SNAPSHOT.reset(token)

    await record_audit(
        session,
        username=user.username,
        role=user.role,
        action="agent_approval_decide",
        target_type="agent_approval",
        target_id=approval_id,
        detail=f"decision={payload.decision}; run={record.run_id}",
    )
    return ApiResponse.ok(_run_out(runtime, record.run_id))


# ----------------------------------------------------------------------
# 评测产物契约（WP-05/WP-12 产出，只读，路径隔离）
# ----------------------------------------------------------------------
# 只允许读取仓库根 `artifacts/agent_evals/latest_v2.json` 这一条路径：
# 不写、不扫描其它产物目录；缺失/损坏/结构不合法一律 6008，绝不构造占位指标。
#
# 下面 EVAL_* 常量镜像 WP-05/WP-12 `backend/tests/agent_evals/contract.py` 的冻结清单
# （REQUIRED_*），由 test_agent_api.py 的防漂移对账测试逐项锁定；
# 评测端新增字段必须先改 contract.py + 本常量，防止消费端静默漂移。
_EVAL_JSON_PATH = (
    Path(__file__).resolve().parents[4] / "artifacts" / "agent_evals" / "latest_v2.json"
)
_EVAL_SOURCE = "artifacts/agent_evals/latest_v2.json"

EVAL_SCHEMA_VERSION = "2.0"
EVAL_REPORT_TYPE = "agent_evals"
EVAL_EVIDENCE_LEVEL = "E1"
EVAL_REQUIRED_TOP_LEVEL = [
    "schema_version",
    "report_type",
    "command",
    "date",
    "code_version",
    "config",
    "evidence_level",
    "environment",
    "sample_size",
    "skipped_count",
    "scenarios",
    "metrics",
]
EVAL_REQUIRED_CODE_VERSION = ["source", "value", "fingerprint", "note"]
EVAL_REQUIRED_CONFIG = ["hash", "scenario_count", "scenario_ids", "files"]
EVAL_REQUIRED_SCENARIO = [
    "id",
    "name",
    "description",
    "evidence_level",
    "expected",
    "passed",
    "skipped",
    "skip_reason",
    "actual",
    "notes",
]
EVAL_REQUIRED_EXPECTED = ["status", "error_code", "shape"]
EVAL_REQUIRED_ACTUAL = [
    "status",
    "error_code",
    "termination_reason",
    "step_count",
    "step_types",
    "tool_executions",
    "decision_latency_ms",
]
EVAL_REQUIRED_METRICS = [
    "success_rate",
    "policy_violation_rate",
    "tool_correct_rate",
    "invalid_loop_rate",
    "recovery_success_rate",
    "p95_decision_latency_ms",
    "restart_recovery_rate",
    "idempotency_conflict_rate",
    "model_fallback_rate",
    "model_schema_rejection_rate",
    "trace_replay_match_rate",
    "approval_handoff_success_rate",
    "device_fault_recovery_rate",
]
EVAL_REQUIRED_METRIC_DEFINITIONS = EVAL_REQUIRED_METRICS
EVAL_REQUIRED_DENOMINATORS = [
    "scenarios",
    "passed_scenarios",
    "tool_calls",
    "tool_calls_correct",
    "tool_executions",
    "policy_violations",
    "invalid_loop_runs",
    "recovery_attempted",
    "recovery_succeeded",
    "restart_attempted",
    "restart_succeeded",
    "idem_conflict_attempts",
    "idem_conflicts_detected",
    "model_attempts",
    "model_output_attempts",
    "model_fallbacks",
    "model_schema_rejections",
    "replay_total_steps",
    "replay_matched_steps",
    "approval_handoff_attempted",
    "approval_handoff_succeeded",
    "device_faults_injected",
    "device_faults_recovered",
]

_EVAL_TERMINAL_STATUSES = set(TERMINAL_STATUSES)
_EVAL_ERROR_CODES = set(ERROR_CODES)


def _validate_eval_payload(payload: Any) -> list[str]:
    """按 WP-05/WP-12 冻结契约严格校验评测产物（空 = 合法）。

    语义对齐 `contract.validate_latest_json`：缺必填字段、版本/类型/
    evidence_level 不符、sample_size 与已执行场景数不一致、
    environment 离线标志非 false、指标值域非法等 → 不可消费，
    调用方返回 6008（不得静默展示占位或全零指标）。
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["评测报告必须是 JSON 对象"]

    for key in EVAL_REQUIRED_TOP_LEVEL:
        if key not in payload:
            errors.append(f"缺少顶层字段 {key}")
    if payload.get("schema_version") != EVAL_SCHEMA_VERSION:
        errors.append(f"schema_version 必须为 {EVAL_SCHEMA_VERSION!r}")
    if payload.get("report_type") != EVAL_REPORT_TYPE:
        errors.append(f"report_type 必须为 {EVAL_REPORT_TYPE!r}")
    if payload.get("evidence_level") != EVAL_EVIDENCE_LEVEL:
        errors.append(
            f"evidence_level 必须为 {EVAL_EVIDENCE_LEVEL!r}"
            "（E1 纪律，禁止 E3/E4）"
        )

    code_version = payload.get("code_version")
    if not isinstance(code_version, dict):
        errors.append("code_version 必须是对象")
    else:
        for key in EVAL_REQUIRED_CODE_VERSION:
            if key not in code_version:
                errors.append(f"code_version 缺少字段 {key}")
        if not code_version.get("value"):
            errors.append("code_version.value 不能为空")
        fingerprint = code_version.get("fingerprint")
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(ch not in "0123456789abcdef" for ch in fingerprint)
        ):
            errors.append("code_version.fingerprint 必须是 64 位十六进制 sha256")

    config = payload.get("config")
    if not isinstance(config, dict):
        errors.append("config 必须是对象")
    else:
        for key in EVAL_REQUIRED_CONFIG:
            if key not in config:
                errors.append(f"config 缺少字段 {key}")
        config_hash = config.get("hash")
        if (
            not isinstance(config_hash, str)
            or len(config_hash) != 64
            or any(ch not in "0123456789abcdef" for ch in config_hash)
        ):
            errors.append("config.hash 必须是 64 位十六进制 sha256")
        scenario_count = config.get("scenario_count")
        if (
            not isinstance(scenario_count, int)
            or isinstance(scenario_count, bool)
            or scenario_count < 0
        ):
            errors.append("config.scenario_count 必须是非负整数")
        scenario_ids = config.get("scenario_ids")
        if not isinstance(scenario_ids, list) or any(
            not isinstance(item, str) for item in scenario_ids
        ):
            errors.append("config.scenario_ids 必须全是字符串")

    environment = payload.get("environment")
    if not isinstance(environment, dict):
        errors.append("environment 必须是对象")
    else:
        for key in ("network_access", "model_access", "device_access"):
            if environment.get(key) is not False:
                errors.append(
                    f"environment.{key} 必须为 false"
                    "（评测禁止访问真实网络/模型/设备）"
                )

    sample_size = payload.get("sample_size")
    if (
        not isinstance(sample_size, int)
        or isinstance(sample_size, bool)
        or sample_size < 0
    ):
        errors.append("sample_size 必须是非负整数")
    skipped_count = payload.get("skipped_count")
    if (
        not isinstance(skipped_count, int)
        or isinstance(skipped_count, bool)
        or skipped_count < 0
    ):
        errors.append("skipped_count 必须是非负整数")

    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, list):
        errors.append("scenarios 必须是数组")
    else:
        valid_scenarios = [item for item in scenarios if isinstance(item, dict)]
        executed = [item for item in valid_scenarios if item.get("skipped") is not True]
        skipped = [item for item in valid_scenarios if item.get("skipped") is True]
        if (
            isinstance(sample_size, int)
            and not isinstance(sample_size, bool)
            and sample_size >= 0
            and len(executed) != sample_size
        ):
            errors.append(
                f"sample_size={sample_size} 与已执行场景数 {len(executed)} 不一致"
            )
        if (
            isinstance(skipped_count, int)
            and not isinstance(skipped_count, bool)
            and skipped_count >= 0
            and len(skipped) != skipped_count
        ):
            errors.append(
                f"skipped_count={skipped_count} 与跳过场景数 {len(skipped)} 不一致"
            )

        seen: set[str] = set()
        for index, scenario in enumerate(scenarios):
            if not isinstance(scenario, dict):
                errors.append(f"scenarios[{index}] 必须是对象")
                continue
            for key in EVAL_REQUIRED_SCENARIO:
                if key not in scenario:
                    errors.append(f"scenarios[{index}] 缺少字段 {key}")
            scenario_id = scenario.get("id")
            if not isinstance(scenario_id, str) or not scenario_id:
                errors.append(f"scenarios[{index}].id 必须是非空字符串")
            elif scenario_id in seen:
                errors.append(f"scenarios[{index}].id 重复：{scenario_id}")
            seen.add(scenario_id)

            if scenario.get("evidence_level") != EVAL_EVIDENCE_LEVEL:
                errors.append(f"scenarios[{index}].evidence_level 必须为 E1")
            if not isinstance(scenario.get("passed"), bool):
                errors.append(f"scenarios[{index}].passed 必须是布尔值")
            if not isinstance(scenario.get("skipped"), bool):
                errors.append(f"scenarios[{index}].skipped 必须是布尔值")
            if scenario.get("skipped") is True:
                if scenario.get("passed") is not False:
                    errors.append(f"scenarios[{index}] 跳过场景 passed 必须为 false")
                if (
                    not isinstance(scenario.get("skip_reason"), str)
                    or not scenario["skip_reason"]
                ):
                    errors.append(
                        f"scenarios[{index}] 跳过场景必须有非空 skip_reason"
                    )
            elif scenario.get("skip_reason") is not None:
                errors.append(
                    f"scenarios[{index}] 非跳过场景 skip_reason 必须为 null"
                )

            for section, keys in (
                ("expected", EVAL_REQUIRED_EXPECTED),
                ("actual", EVAL_REQUIRED_ACTUAL),
            ):
                node = scenario.get(section)
                if not isinstance(node, dict):
                    errors.append(f"scenarios[{index}].{section} 必须是对象")
                else:
                    for key in keys:
                        if key not in node:
                            errors.append(
                                f"scenarios[{index}].{section} 缺少字段 {key}"
                            )

            expected = scenario.get("expected")
            if isinstance(expected, dict):
                if expected.get("status") not in _EVAL_TERMINAL_STATUSES:
                    errors.append(f"scenarios[{index}].expected.status 不是冻结终态")
                expected_error = expected.get("error_code")
                if (
                    expected_error is not None
                    and expected_error not in _EVAL_ERROR_CODES
                ):
                    errors.append(
                        f"scenarios[{index}].expected.error_code 不是冻结错误码"
                    )

            actual = scenario.get("actual")
            if isinstance(actual, dict):
                if scenario.get("skipped") is True:
                    if actual.get("status") is not None:
                        errors.append(
                            f"scenarios[{index}] 跳过场景 actual.status 必须为 null"
                        )
                elif actual.get("status") not in _EVAL_TERMINAL_STATUSES:
                    errors.append(f"scenarios[{index}].actual.status 不是冻结终态")
                actual_error = actual.get("error_code")
                if actual_error is not None and actual_error not in _EVAL_ERROR_CODES:
                    errors.append(
                        f"scenarios[{index}].actual.error_code 不是冻结错误码"
                    )
                if scenario.get("skipped") is not True:
                    if not isinstance(actual.get("step_count"), int) or isinstance(
                        actual.get("step_count"), bool
                    ):
                        errors.append(
                            f"scenarios[{index}].actual.step_count 必须是整数"
                        )
                    if not isinstance(actual.get("step_types"), list):
                        errors.append(
                            f"scenarios[{index}].actual.step_types 必须是数组"
                        )
                    if not isinstance(actual.get("tool_executions"), dict):
                        errors.append(
                            f"scenarios[{index}].actual.tool_executions 必须是对象"
                        )
                    latency = actual.get("decision_latency_ms")
                    if (
                        not isinstance(latency, (int, float))
                        or isinstance(latency, bool)
                        or latency < 0
                    ):
                        errors.append(
                            f"scenarios[{index}].actual.decision_latency_ms"
                            " 必须是非负数"
                        )
            if not isinstance(scenario.get("notes"), list):
                errors.append(f"scenarios[{index}].notes 必须是数组")

    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        errors.append("metrics 必须是对象")
    else:
        for key in EVAL_REQUIRED_METRICS:
            if key not in metrics:
                errors.append(f"metrics 缺少聚合指标 {key}")
            elif key == "p95_decision_latency_ms":
                value = metrics[key]
                if value is not None and (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or value < 0
                ):
                    errors.append(
                        f"metrics.p95_decision_latency_ms 值域非法：{value!r}"
                    )
            else:
                value = metrics[key]
                if value is not None and (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not 0.0 <= float(value) <= 1.0
                ):
                    errors.append(
                        f"metrics.{key} 值域非法（必须为 null 或 [0,1]）：{value!r}"
                    )
        definitions = metrics.get("definitions")
        if not isinstance(definitions, dict):
            errors.append("metrics.definitions 必须是对象（口径文档）")
        else:
            for key in EVAL_REQUIRED_METRIC_DEFINITIONS:
                if not isinstance(definitions.get(key), str) or not definitions[key]:
                    errors.append(f"metrics.definitions 缺少 {key} 的口径")
        denominators = metrics.get("denominators")
        if not isinstance(denominators, dict):
            errors.append("metrics.denominators 必须是对象（分母台账）")
        else:
            for key in EVAL_REQUIRED_DENOMINATORS:
                if key not in denominators:
                    errors.append(f"metrics.denominators 缺少 {key}")
                elif (
                    not isinstance(denominators[key], int)
                    or isinstance(denominators[key], bool)
                    or denominators[key] < 0
                ):
                    errors.append(
                        f"metrics.denominators.{key} 必须是非负整数"
                    )
    return errors


def _eval_scenario_out(sc: dict[str, Any]) -> AgentEvalScenarioOut:
    return AgentEvalScenarioOut(
        id=sc["id"],
        name=sc.get("name"),
        description=sc.get("description"),
        evidence_level=sc.get("evidence_level"),
        passed=sc["passed"],
        skipped=sc["skipped"],
        skip_reason=sc.get("skip_reason"),
        expected=AgentEvalExpectedOut(**sc["expected"]) if sc.get("expected") else None,
        actual=AgentEvalActualOut(**sc["actual"]) if sc.get("actual") else None,
        notes=list(sc.get("notes") or []),
    )


def _eval_metrics_out(metrics: dict[str, Any]) -> AgentEvalMetricsOut:
    return AgentEvalMetricsOut(
        success_rate=metrics.get("success_rate"),
        policy_violation_rate=metrics.get("policy_violation_rate"),
        tool_correct_rate=metrics.get("tool_correct_rate"),
        invalid_loop_rate=metrics.get("invalid_loop_rate"),
        recovery_success_rate=metrics.get("recovery_success_rate"),
        p95_decision_latency_ms=metrics.get("p95_decision_latency_ms"),
        restart_recovery_rate=metrics.get("restart_recovery_rate"),
        idempotency_conflict_rate=metrics.get("idempotency_conflict_rate"),
        model_fallback_rate=metrics.get("model_fallback_rate"),
        model_schema_rejection_rate=metrics.get("model_schema_rejection_rate"),
        trace_replay_match_rate=metrics.get("trace_replay_match_rate"),
        approval_handoff_success_rate=metrics.get("approval_handoff_success_rate"),
        device_fault_recovery_rate=metrics.get("device_fault_recovery_rate"),
        business_success_rate=metrics.get("business_success_rate"),
        definitions=dict(metrics.get("definitions") or {}),
        denominators=dict(metrics.get("denominators") or {}),
    )


def _eval_out(payload: dict[str, Any]) -> AgentEvalOut:
    """把已通过契约校验的产物映射为最小自有数据结构（不序列化内部对象）。"""
    return AgentEvalOut(
        available=True,
        report_type=payload["report_type"],
        schema_version=payload["schema_version"],
        evidence_level=payload["evidence_level"],
        command=payload.get("command"),
        date=payload.get("date"),
        code_version=dict(payload.get("code_version") or {}),
        config=dict(payload.get("config") or {}),
        environment=dict(payload.get("environment") or {}),
        sample_size=int(payload["sample_size"]),
        skipped_count=int(payload["skipped_count"]),
        scenarios=[_eval_scenario_out(item) for item in payload.get("scenarios") or []],
        metrics=_eval_metrics_out(payload.get("metrics") or {}),
        source=_EVAL_SOURCE,
    )


@router.get(
    "/evals/latest",
    response_model=ApiResponse[AgentEvalOut],
    summary="最新固定场景评测结果",
)
async def latest_eval(_user: CurrentUser = Depends(get_current_user)):
    """读取 WP-05/WP-12 产物 artifacts/agent_evals/latest_v2.json。

    严格校验契约（镜像 contract.py 冻结清单）：产物缺失 / JSON
    损坏 / 缺必填字段 / sample_size 与已执行场景数不一致 /
    evidence_level 不符 → 6008 空态，绝不把占位或全零指标当作真实评测展示。
    """
    path = _EVAL_JSON_PATH
    if not path.is_file():
        raise AppException(
            code=AGENT_EVAL_UNAVAILABLE,
            message="尚无 Agent 评测结果，请先运行固定场景评测（WP-05/WP-12）",
            http_status=200,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppException(
            code=AGENT_EVAL_UNAVAILABLE,
            message=f"Agent 评测结果不可读：{exc}",
            http_status=200,
        ) from exc

    errors = _validate_eval_payload(payload)
    if errors:
        detail = "；".join(errors[:8]) + ("…" if len(errors) > 8 else "")
        raise AppException(
            code=AGENT_EVAL_UNAVAILABLE,
            message=f"Agent 评测结果不符合契约（{detail}）",
            http_status=200,
        )

    return ApiResponse.ok(_eval_out(payload))
