"""对话助手工具集（异步、只读为主）。

与「事件处置 Agent」的 ToolRegistry（app/services/agents/tools.py：
同步 handler + contextvar 事件快照，派单状态机专用）是**两套独立抽象**。
这里的工具直接访问 AsyncSession / CvDetector，供 LLM 工具编排与规则兜底
共用同一个实现 —— 规则路径与模型路径看到的数据必然一致。

设计要点：
- 每个工具声明 name / description / parameters(JSON Schema) / risk_level，
  既作为发给模型的 function 描述，也作为参数白名单校验依据。
- **全部只读**：派单只生成建议卡，确认走既有 POST /agents/runs
  （含审批、审计、事件快照），对话层不写业务库。
- ``image.analyze`` 由引擎在用户上传图片时**直接调用**（model_visible=False），
  不暴露给模型 —— 图片二进制不该经模型转发，且检测必须发生、
  不能由模型自由裁量。
- 复用 app.services.agents.schema.validate_schema 做参数校验，
  与 Agent 侧保持同一套 JSON Schema 语义。
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import EventStatus, WasteClass
from app.repositories import DeviceRepository, EventRepository, TaskRepository
from app.services.agents.schema import validate_schema

# edge/detector 在 seahawk/ 根下，而 backend 运行时 sys.path 是 backend/。
# 与 app/api/v1/ai.py 一致：把 seahawk 根加入 sys.path 才能 import CvDetector。
_SEAHAWK_ROOT = Path(__file__).resolve().parents[4]
if str(_SEAHAWK_ROOT) not in sys.path:
    sys.path.insert(0, str(_SEAHAWK_ROOT))

# 事件状态中文标签（与 app/api/v1/events.py 的 _to_out 保持一致，
# 避免对话卡片与事件列表页同一状态两种叫法）
_EVENT_STATUS_LABELS = {
    "new": "待处理",
    "dispatched": "已派单",
    "resolved": "已清理",
    "ignored": "已忽略",
}

# 工具执行错误码（模块内部使用，不进 HTTP 层 —— 工具失败回灌给模型/规则路径，
# 由引擎转化为对用户友好的回复，绝不直接暴露内部错误细节）
TOOL_UNKNOWN = "tool_unknown"
TOOL_DENIED = "tool_denied"
TOOL_ARGS_INVALID = "tool_args_invalid"
TOOL_EXECUTION_FAILED = "tool_execution_failed"


class AssistantToolError(Exception):
    """对话工具执行异常（语义化 code，供引擎转化为 tool_results 错误项）。"""

    def __init__(self, message: str, *, code: str = TOOL_EXECUTION_FAILED) -> None:
        self.code = code
        super().__init__(message)


ToolHandler = Callable[[AsyncSession, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class AssistantTool:
    """对话工具描述（JSON Schema 的 properties 即模型可用参数白名单）。

    model_visible=False 的工具不发给模型（如 image.analyze），由引擎直调。
    """

    name: str
    description: str
    parameters: dict[str, Any]
    risk_level: str
    handler: ToolHandler
    model_visible: bool = True


TOOLS: dict[str, AssistantTool] = {}


def _register(tool: AssistantTool) -> AssistantTool:
    TOOLS[tool.name] = tool
    return tool


def tool_descriptors() -> list[dict[str, Any]]:
    """发给模型的 function 描述（仅 model_visible 工具）。"""
    return [
        {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
            "risk_level": t.risk_level,
        }
        for t in TOOLS.values()
        if t.model_visible
    ]


# ----------------------------------------------------------------------
# 序列化助手
# ----------------------------------------------------------------------
async def _event_coords(session: AsyncSession, event) -> tuple[float, float]:
    """从 PostGIS POINT 取经纬度（与 events.py 的取法一致）。"""
    row = (
        await session.execute(
            select(func.ST_X(event.location), func.ST_Y(event.location))
        )
    ).first()
    lng = float(row[0]) if row and row[0] is not None else 0.0
    lat = float(row[1]) if row and row[1] is not None else 0.0
    return lng, lat


def _event_card(event, lng: float, lat: float) -> dict[str, Any]:
    """ORM → 事件卡字典（对话 block 载荷，字段与 EventOut 对齐）。"""
    return {
        "event_id": event.event_id,
        "device_id": event.device_id,
        "event_time": event.event_time.isoformat() if event.event_time else None,
        "lng": lng,
        "lat": lat,
        "main_class": event.main_class,
        "main_class_label": WasteClass.LABELS.get(event.main_class),
        "det_count": event.det_count,
        "max_confidence": float(event.max_confidence),
        "evidence_url": event.evidence_url,
        "status": event.status,
        "status_label": _EVENT_STATUS_LABELS.get(event.status),
    }


# ----------------------------------------------------------------------
# 工具实现
# ----------------------------------------------------------------------
async def _image_analyze(session: AsyncSession, args: dict[str, Any]) -> dict[str, Any]:
    """OpenCV 检测一张海漂垃圾图片（复用 edge.detector，输出契约同事件上报）。

    图片二进制经 ``args["image_bytes"]`` 由引擎注入（不是模型传参）。
    """
    del session  # 检测不查库

    # cv2/numpy 只在真正分析图片时才 import，避免缺依赖时影响整个模块导入
    import cv2
    import numpy as np
    from edge.detector.detector import CLASS_NAMES, CvDetector

    image_bytes = args.get("image_bytes")
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        raise AssistantToolError("图片内容为空", code=TOOL_ARGS_INVALID)

    nparr = np.frombuffer(bytes(image_bytes), np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        raise AssistantToolError("无法解析图片（非 jpg/png）", code=TOOL_ARGS_INVALID)

    # 检测是 CPU 密集 + 同步，放进线程避免阻塞事件循环
    detector = CvDetector()
    detections = await asyncio.to_thread(detector.detect, img)

    return {
        "detections": detections,
        "count": len(detections),
        "width": int(img.shape[1]),
        "height": int(img.shape[0]),
        "classes": list(CLASS_NAMES),
        "engine": "opencv",
    }


async def _event_list(session: AsyncSession, args: dict[str, Any]) -> dict[str, Any]:
    """查询最近 N 小时的事件列表（只读）。"""
    repo = EventRepository(session)
    hours = int(args.get("hours", 24))
    main_class = args.get("main_class") or None
    status = args.get("status") or None
    limit = int(args.get("limit", 10))
    events, total = await repo.list_events(
        hours=hours, main_class=main_class, status=status, limit=limit
    )
    items = []
    for event in events:
        lng, lat = await _event_coords(session, event)
        items.append(_event_card(event, lng, lat))
    return {"items": items, "total": total, "hours": hours}


async def _event_get(session: AsyncSession, args: dict[str, Any]) -> dict[str, Any]:
    """按 event_id 查单个事件（只读）；不存在返回 found=False 而非抛异常。"""
    event_id = args.get("event_id")
    event = await EventRepository(session).get_by_event_id(event_id)
    if event is None:
        return {"found": False, "event_id": event_id}
    lng, lat = await _event_coords(session, event)
    return {"found": True, "event": _event_card(event, lng, lat)}


async def _stats_overview(session: AsyncSession, args: dict[str, Any]) -> dict[str, Any]:
    """平台运营指标总览（只读）。"""
    del args
    event_repo = EventRepository(session)
    task_repo = TaskRepository(session)
    device_repo = DeviceRepository(session)
    return {
        "event_count_24h": await event_repo.count_since(hours=24),
        "tasks_by_status": await task_repo.count_by_status(),
        "done_tasks_24h": await task_repo.done_count_since(hours=24),
        "collected_kg_total": round(await task_repo.sum_collected_weight(), 3),
        "devices_by_status": await device_repo.count_by_status(),
    }


async def _dispatch_suggest(session: AsyncSession, args: dict[str, Any]) -> dict[str, Any]:
    """就近可用机器人候选（只读建议，不创建工单）。

    事件不存在返回结构化 found=False（不抛异常）—— 对话层要让模型/规则
    能把「事件不存在」如实告诉用户，而不是变成一次工具崩溃。
    """
    event_id = args.get("event_id")
    event = await EventRepository(session).get_by_event_id(event_id)
    if event is None:
        return {
            "event_id": event_id,
            "found": False,
            "candidates": [],
            "hint": "事件不存在",
        }

    robots = await DeviceRepository(session).find_nearest_available_robots(
        event_id, limit=3
    )
    candidates = [
        {
            "device_id": d.device_id,
            "name": d.name,
            "status": d.status,
            "distance_m": round(dist, 1),
        }
        for d, dist in robots
    ]
    if candidates:
        hint = "确认派单将创建工单并进入审批流程"
    else:
        hint = "5km 内暂无在线可用机器人"
    lng, lat = await _event_coords(session, event)
    return {
        "event_id": event_id,
        "found": True,
        "event": _event_card(event, lng, lat),
        "candidates": candidates,
        "hint": hint,
    }


# ----------------------------------------------------------------------
# 工具注册
# ----------------------------------------------------------------------
_register(
    AssistantTool(
        name="image.analyze",
        description=(
            "对一张海漂垃圾图片做 OpenCV 检测，返回检测框/类别/置信度。"
            "图片二进制由系统注入（image_bytes），模型不可直接调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "image_bytes": {
                    "type": "string",
                    "description": "图片二进制（由系统注入，非模型传参）",
                },
            },
            "required": ["image_bytes"],
        },
        risk_level="read",
        handler=_image_analyze,
        model_visible=False,
    )
)

_register(
    AssistantTool(
        name="event.list",
        description="查询最近 N 小时的海漂垃圾事件列表（可按类别/状态筛选），返回事件卡片数组与总数。",
        parameters={
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 720,
                    "description": "时间窗口（小时），默认 24",
                },
                "main_class": {
                    "type": "string",
                    "enum": list(WasteClass.ALL),
                    "description": "类别筛选",
                },
                "status": {
                    "type": "string",
                    "enum": list(EventStatus.ALL),
                    "description": "状态筛选",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "返回条数，默认 10",
                },
            },
        },
        risk_level="read",
        handler=_event_list,
    )
)

_register(
    AssistantTool(
        name="event.get",
        description="按 event_id 查询单个海漂垃圾事件的详情（坐标/类别/状态/证据帧）。",
        parameters={
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": "事件编号",
                },
            },
            "required": ["event_id"],
        },
        risk_level="read",
        handler=_event_get,
    )
)

_register(
    AssistantTool(
        name="stats.overview",
        description=(
            "平台运营指标总览：24h 事件数、各状态任务数、24h 完成数、"
            "累计清理重量、各状态设备数。"
        ),
        parameters={"type": "object", "properties": {}},
        risk_level="read",
        handler=_stats_overview,
    )
)

_register(
    AssistantTool(
        name="dispatch.suggest",
        description=(
            "针对某个事件给出就近可用机器人候选（只读建议，不创建工单；"
            "确认派单走 /agents/runs 审批流程）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": "事件编号",
                },
            },
            "required": ["event_id"],
        },
        risk_level="read",
        handler=_dispatch_suggest,
    )
)


async def run_tool(
    name: str,
    session: AsyncSession,
    args: dict[str, Any],
    *,
    via_model: bool = False,
) -> dict[str, Any]:
    """执行一个对话工具。

    via_model=True 时做模型侧的额外防护：工具必须对模型可见、参数白名单
    校验、JSON Schema 校验。引擎直调（image.analyze）走 via_model=False，
    跳过这些仅针对不可信输入的校验。
    """
    tool = TOOLS.get(name)
    if tool is None:
        raise AssistantToolError(f"未知工具 {name}", code=TOOL_UNKNOWN)

    if via_model:
        if not tool.model_visible:
            raise AssistantToolError(
                f"工具 {name} 不允许模型直接调用", code=TOOL_DENIED
            )
        # 参数白名单：JSON Schema 的 properties 即模型可传参数集合
        allowed = set(tool.parameters.get("properties", {}))
        unknown = sorted(set(args) - allowed)
        if unknown:
            raise AssistantToolError(
                f"工具 {name} 收到未声明参数 {unknown}", code=TOOL_ARGS_INVALID
            )
        errors = validate_schema(args, tool.parameters)
        if errors:
            raise AssistantToolError(
                f"工具 {name} 参数校验失败: {'; '.join(errors)}",
                code=TOOL_ARGS_INVALID,
            )

    try:
        return await tool.handler(session, args)
    except AssistantToolError:
        raise
    except ImportError as exc:
        # 依赖缺失要走单独的分支：这类失败不是"工具算错了"，
        # 而是部署缺件（cv2/opencv 未装、edge/ 未挂进容器）。
        # 统一报 "KeyError/ModuleNotFoundError 执行失败" 会让线上排查绕远路。
        raise AssistantToolError(
            f"工具 {name} 依赖缺失（{exc}）—— 请检查镜像是否安装 opencv/numpy，"
            "以及 edge/ 目录是否已挂载到容器",
            code=TOOL_EXECUTION_FAILED,
        ) from exc
    except Exception as exc:  # noqa: BLE001 —— 工具崩溃不能打穿对话，转成语义错误
        raise AssistantToolError(
            f"工具 {name} 执行失败: {type(exc).__name__}",
            code=TOOL_EXECUTION_FAILED,
        ) from exc
