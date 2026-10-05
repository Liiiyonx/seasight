"""对话引擎：会话管理 + 模型工具编排 + 规则兜底。

主流程 ``chat()``：
1. enabled 检查（配置关闭 → 8004，不假装可用）
2. 取/建会话（session_id 提供但不存在**或不属于当前用户** → 8001，
   不泄露他人会话存在性；匿名用户共享 anonymous 命名空间，演示友好）
3. 落库 user 消息（content_type = image / text）
4. 有图片**必跑** image.analyze（引擎直调，不经模型 —— 检测必须发生，
   单图失败产 error block 不崩溃）
5. 模型三要素齐 + enabled → 工具编排循环（任何失败回退规则路径）；
   否则规则意图识别（派单/统计/查单/查列表/能力说明）
6. 落库 assistant 消息（payload = blocks JSON），返回 AssistantReply

安全纪律（与 app.services.agents.model_adapter 一致）：
- 发给模型的 payload 整体过 ``_sanitize_chat``（SENSITIVE_KEYS 键丢弃、
  超长截断），会话历史里绝不夹带密钥类字段
- 每轮模型输出先过 FORBIDDEN_FIELD_NAMES 检查（思维链/推理字段）
- 日志只记轮数 / 异常类型 / 响应哈希前缀，绝不记完整 prompt / 原始回复
- 未配模型 → ``fallback=True``、``model_used="rule-fallback"``，绝不假装有模型
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import CurrentUser
from app.core.exceptions import AppException, ErrorCode
from app.models.chat import ChatMessage, ChatRole, ChatSession
from app.models.event import WasteClass
from app.services.agents.model_adapter import (
    FORBIDDEN_FIELD_NAMES,
    SENSITIVE_KEYS,
    ModelAdapterError,
    OpenAICompatibleModelClient,
)
from app.services.assistant import (
    CHAT_ASSISTANT_DISABLED,
    CHAT_SESSION_NOT_FOUND,
)
from app.services.assistant.tools import (
    AssistantToolError,
    run_tool,
    tool_descriptors,
)

_LOG = logging.getLogger("oceanus.assistant.engine")

# ----------------------------------------------------------------------
# 规则意图词表（冻结在本模块：规则兜底是「未配模型时的诚实降级」，
# 词表集中在此便于审阅；模型路径不读这些词表）
# ----------------------------------------------------------------------
_DISPATCH_WORDS = ("派单", "处置", "清理", "处理", "派机器人", "打捞")
_STATS_WORDS = ("多少", "统计", "汇总", "几个", "数量", "总量", "大屏", "指标")
_QUERY_WORDS = ("事件", "最新", "列表", "最近", "查询", "看看")

# 事件编号形如 evt_demo_0001 / EVT-0001；\w 已含下划线与数字
_EVENT_ID_RE = re.compile(r"(evt[_\-]?\w+)", re.IGNORECASE)

# ----------------------------------------------------------------------
# 对话 payload 脱敏阈值：model_adapter 的 200 字符上限是给「工具参数 /
# 事件快照」定的，对话消息天然更长，这里放宽到 2000 —— 脱敏的本意是
# 挡密钥类字段与失控体积，不是把正常对话砍成摘要。
# ----------------------------------------------------------------------
_MAX_CHAT_STR_LEN = 2000
_MAX_CHAT_ITEMS = 50
_MAX_CHAT_DEPTH = 4

# 对话专用 system prompt（与派单规划的 DEFAULT_MODEL_SYSTEM_PROMPT 独立：
# 那是英文规划器，这里是中文对话助手）
_CHAT_SYSTEM_PROMPT = (
    "你是「探海灵眸 Oceanus」海漂垃圾监测平台的对话助手。"
    "能力：查询海漂垃圾事件、查看平台运营统计、对事件给出就近派单建议；"
    "用户上传的图片由系统直接检测并把结果放进对话，你不需请求。"
    "规则：派单只生成建议，最终确认由用户在前端完成；"
    "不要编造不存在的事件编号或机器人；"
    "回答用简体中文，简洁口语化；不确定就直说不确定。"
    "你只输出一个 JSON 对象，格式见 output_protocol，不要输出其他内容。"
)

_OUTPUT_PROTOCOL = {
    "instruction": (
        "只输出一个 JSON 对象，二选一："
        '{"reply": "<给用户的最终回复>"} 或 '
        '{"tool_call": {"name": "<工具名>", "arguments": {...}}}。'
        "需要数据时输出 tool_call，系统执行后把结果放进 tool_results 再问；"
        "信息足够时输出 reply。不要输出任何其他键。"
    ),
    "reply_key": "reply",
    "tool_call_key": "tool_call",
}

_CAPABILITY_TEXT = (
    "我是探海灵眸对话助手，可以帮你：\n"
    "· 查询海漂垃圾事件（如「最新事件列表」）\n"
    "· 查看平台运营统计（如「今天有多少事件」）\n"
    "· 对事件给出就近派单建议（如「evt_xxx 派单」）\n"
    "· 拖入或粘贴图片，自动做海漂垃圾检测"
)


class _ModelOutputInvalid(Exception):
    """模型单轮输出不可用（非法 JSON / 缺键 / 禁字段）→ 终止模型路径回退规则。"""


@dataclass
class AssistantReply:
    """``chat()`` 的返回值（assistant 消息已落库）。"""

    message_id: str
    session_id: str
    text: str
    blocks: list[dict[str, Any]] = field(default_factory=list)
    model_used: str = "rule-fallback"
    fallback: bool = True


# ----------------------------------------------------------------------
# 编号与脱敏
# ----------------------------------------------------------------------
def new_session_id() -> str:
    """会话业务编号 cs_YYYYMMDD_hex6（API 层建空会话也复用本函数）。"""
    return f"cs_{datetime.now():%Y%m%d}_{uuid.uuid4().hex[:6]}"


def _new_message_id() -> str:
    return f"cm_{datetime.now():%Y%m%d}_{uuid.uuid4().hex[:6]}"


def _sanitize_chat(value: Any, depth: int = 0) -> Any:
    """递归脱敏（语义同 ModelAdapterPlanner._sanitize，阈值按对话放宽）。"""
    if depth > _MAX_CHAT_DEPTH:
        return "<depth>"
    if isinstance(value, str):
        return (
            value
            if len(value) <= _MAX_CHAT_STR_LEN
            else value[: _MAX_CHAT_STR_LEN - 3] + "..."
        )
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, sub in value.items():
            if not isinstance(key, str) or key.lower() in SENSITIVE_KEYS:
                continue
            if len(out) >= _MAX_CHAT_ITEMS:
                out["<truncated>"] = True
                break
            out[key] = _sanitize_chat(sub, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        items = [_sanitize_chat(item, depth + 1) for item in value[:_MAX_CHAT_ITEMS]]
        if len(value) > _MAX_CHAT_ITEMS:
            items.append("<truncated>")
        return items
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__name__}>"


def _reject_forbidden_fields(value: Any) -> None:
    """递归拒绝思维链/推理字段（大小写不敏感）；错误消息不回显字段名。"""
    if isinstance(value, dict):
        for key, sub in value.items():
            if isinstance(key, str) and key.lower() in FORBIDDEN_FIELD_NAMES:
                raise _ModelOutputInvalid("模型输出包含被禁止字段")
            _reject_forbidden_fields(sub)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_forbidden_fields(item)


def _model_configured() -> bool:
    """模型三要素（端点 + 模型名；api_key 对本地模型可空）是否配齐。"""
    return bool(
        settings.assistant_enabled
        and settings.assistant_model_base_url.strip()
        and settings.assistant_model_name.strip()
    )


# ----------------------------------------------------------------------
# 会话与历史
# ----------------------------------------------------------------------
async def _get_or_create_session(
    session: AsyncSession,
    *,
    user: CurrentUser,
    session_id: str | None,
    text: str,
    has_images: bool,
) -> ChatSession:
    if session_id:
        row = (
            await session.execute(
                select(ChatSession).where(ChatSession.session_id == session_id)
            )
        ).scalar_one_or_none()
        # 不存在或不属于当前用户都报 8001 —— 不泄露他人会话的存在性
        if row is None or row.username != user.username:
            raise AppException(
                code=CHAT_SESSION_NOT_FOUND,
                message="会话不存在",
                http_status=200,
            )
        return row

    title = text.strip()[:20] or ("图片分析" if has_images else "新对话")
    chat_session = ChatSession(
        session_id=new_session_id(),
        username=user.username,
        title=title,
    )
    session.add(chat_session)
    await session.flush()
    return chat_session


async def _load_history(session: AsyncSession, session_id: str) -> list[ChatMessage]:
    """取最近 assistant_history_limit 条历史，按时间正序返回（供模型上下文）。"""
    rows = (
        (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.id.desc())
                .limit(settings.assistant_history_limit)
            )
        )
        .scalars()
        .all()
    )
    return list(reversed(rows))


# ----------------------------------------------------------------------
# 展示块与回复文本构造
# ----------------------------------------------------------------------
def _blocks_from_tool_result(name: str, result: Any) -> list[dict[str, Any]]:
    """工具结果 → 结构化展示块（type 取值冻结在 app.schemas.chat.ChatBlockType）。"""
    if not isinstance(result, dict) or "error" in result:
        return []
    if name == "event.list":
        return [{"type": "event_list", "data": result}]
    if name == "event.get":
        if not result.get("found"):
            return []
        return [{"type": "event_card", "data": result["event"]}]
    if name == "stats.overview":
        return [{"type": "stats", "data": result}]
    if name == "dispatch.suggest":
        return [{"type": "dispatch_suggest", "data": result}]
    return []


def _detection_summary(idx: int, result: dict[str, Any]) -> str:
    """单图检测结果 → 一句中文摘要（模型上下文与规则回复共用）。"""
    count = int(result.get("count") or 0)
    head = f"第{idx + 1}张图"
    if count <= 0:
        return f"{head}：未发现海漂垃圾目标"
    by_class: dict[str, int] = {}
    max_conf = 0.0
    for det in result.get("detections") or []:
        if not isinstance(det, dict):
            continue
        cls = str(det.get("class") or "other")
        by_class[cls] = by_class.get(cls, 0) + 1
        try:
            max_conf = max(max_conf, float(det.get("confidence") or 0))
        except (TypeError, ValueError):
            continue
    parts = "、".join(
        f"{WasteClass.LABELS.get(cls, cls)}×{n}" for cls, n in by_class.items()
    )
    return f"{head}：发现 {count} 个目标（{parts}），最高置信度 {max_conf:.0%}"


def _dispatch_reply_text(result: dict[str, Any]) -> str:
    if not result.get("found"):
        return f"未找到事件 {result.get('event_id')}，请确认编号是否正确。"
    candidates = result.get("candidates") or []
    if not candidates:
        return (
            f"事件 {result['event_id']} 附近 5km 内暂无在线可用机器人，"
            "建议稍后再试或安排人工处置。"
        )
    lines = [f"事件 {result['event_id']} 的就近可用机器人："]
    for c in candidates:
        lines.append(
            f"· {c.get('name') or c['device_id']}（{c['device_id']}），"
            f"距离约 {c.get('distance_m', 0):.0f} 米"
        )
    lines.append("点击下方卡片「确认派单」将创建工单并进入审批流程。")
    return "\n".join(lines)


def _stats_reply_text(result: dict[str, Any]) -> str:
    tasks = result.get("tasks_by_status") or {}
    devices = result.get("devices_by_status") or {}
    in_flight = sum(
        int(tasks.get(s, 0) or 0) for s in ("assigned", "navigating", "collecting")
    )
    devices_total = sum(int(v or 0) for v in devices.values())
    return (
        f"平台概况：24 小时新增事件 {result.get('event_count_24h', 0)} 起；"
        f"任务待派 {int(tasks.get('pending', 0) or 0)} / 进行中 {in_flight}"
        f" / 24h 完成 {result.get('done_tasks_24h', 0)}；"
        f"累计清理 {result.get('collected_kg_total', 0)} kg；"
        f"设备在线 {int(devices.get('online', 0) or 0)} / 共 {devices_total} 台。"
    )


def _event_get_reply_text(result: dict[str, Any]) -> str:
    if not result.get("found"):
        return f"未找到事件 {result.get('event_id')}，请确认编号是否正确。"
    e = result["event"]
    return (
        f"事件 {e['event_id']}：{e.get('main_class_label') or e['main_class']}，"
        f"状态「{e.get('status_label') or e['status']}」，"
        f"时间 {e.get('event_time') or '-'}，"
        f"坐标 ({e.get('lng', 0):.4f}, {e.get('lat', 0):.4f})。"
    )


def _event_list_reply_text(result: dict[str, Any]) -> str:
    items = result.get("items") or []
    hours = result.get("hours", 24)
    if not items:
        return f"最近 {hours} 小时没有查询到事件。"
    lines = [f"最近 {hours} 小时共 {result.get('total', len(items))} 起事件，最新 {len(items)} 条："]
    for e in items:
        lines.append(
            f"· {e['event_id']}｜{e.get('main_class_label') or e['main_class']}"
            f"｜{e.get('status_label') or e['status']}"
        )
    return "\n".join(lines)


def _extract_event_id(text: str) -> str | None:
    match = _EVENT_ID_RE.search(text or "")
    return match.group(1) if match else None


# ----------------------------------------------------------------------
# 规则兜底（五分支：派单 / 统计 / 查单 / 查列表 / 能力说明）
# ----------------------------------------------------------------------
async def _rule_path(
    session: AsyncSession,
    *,
    text: str,
    has_images: bool,
    det_summaries: list[str],
    blocks: list[dict[str, Any]],
) -> str:
    # 图片摘要前缀：检测块已在主流程生成，这里只补一句人话
    prefix = ""
    if has_images:
        prefix = ("；".join(det_summaries) + "。") if det_summaries else "图片无法解析（需要 jpg/png 格式）。"

    event_id = _extract_event_id(text)

    # ① event_id + 派单词 → 派单建议卡
    if event_id and any(w in text for w in _DISPATCH_WORDS):
        result = await run_tool("dispatch.suggest", session, {"event_id": event_id})
        blocks.extend(_blocks_from_tool_result("dispatch.suggest", result))
        return prefix + _dispatch_reply_text(result)

    # ② 统计词 → 指标卡
    if any(w in text for w in _STATS_WORDS):
        result = await run_tool("stats.overview", session, {})
        blocks.extend(_blocks_from_tool_result("stats.overview", result))
        return prefix + _stats_reply_text(result)

    # ③ 裸 event_id → 单个事件卡（用户只敲了编号，想看这条事件）
    if event_id:
        result = await run_tool("event.get", session, {"event_id": event_id})
        blocks.extend(_blocks_from_tool_result("event.get", result))
        return prefix + _event_get_reply_text(result)

    # ④ 查询词 → 事件列表
    if any(w in text for w in _QUERY_WORDS):
        result = await run_tool("event.list", session, {"hours": 24, "limit": 10})
        blocks.extend(_blocks_from_tool_result("event.list", result))
        return prefix + _event_list_reply_text(result)

    # ⑤ 仅图片 → 检测摘要；纯文本未命中 → 能力说明
    if has_images:
        return prefix + "如需对检测结果派单处置，回复事件编号加「派单」即可。"
    return _CAPABILITY_TEXT


# ----------------------------------------------------------------------
# 模型路径（工具编排循环；任何失败返回 None → 调用方回退规则）
# ----------------------------------------------------------------------
async def _chat_with_model(
    session: AsyncSession,
    *,
    chat_session: ChatSession,
    text: str,
    det_summaries: list[str],
    blocks: list[dict[str, Any]],
) -> tuple[str, str] | None:
    """返回 (reply_text, model_name)；失败返回 None。

    契约：绝不把内部异常抛出本函数 —— 模型路径是「锦上添花」，
    任何超时/连接失败/非法输出都必须让对话落到规则兜底上继续可用。
    """
    history = await _load_history(session, chat_session.session_id)
    conversation = [{"role": m.role, "content": m.content} for m in history]

    current_content = text or "[用户上传了图片]"
    if det_summaries:
        current_content += "\n[系统检测] " + "；".join(det_summaries)
    conversation.append({"role": "user", "content": current_content})

    client = OpenAICompatibleModelClient(
        base_url=settings.assistant_model_base_url,
        model=settings.assistant_model_name,
        api_key=settings.assistant_model_api_key,
        timeout_ms=settings.assistant_model_timeout_ms,
        max_output_tokens=settings.assistant_model_max_output_tokens,
        system_prompt=_CHAT_SYSTEM_PROMPT,
    )
    tools = tool_descriptors()
    tool_results: list[dict[str, Any]] = []
    max_rounds = max(1, int(settings.assistant_model_max_tool_rounds))

    for round_idx in range(max_rounds):
        payload = _sanitize_chat(
            {
                "conversation": conversation,
                "tools": tools,
                "tool_results": tool_results,
                "output_protocol": _OUTPUT_PROTOCOL,
            }
        )
        try:
            # complete 是同步 urlopen（CPU 轻、IO 阻塞），放线程避免卡事件循环
            raw = await asyncio.to_thread(client.complete, payload)
            parsed = json.loads(raw)
            _reject_forbidden_fields(parsed)
        except (ModelAdapterError, json.JSONDecodeError, _ModelOutputInvalid) as exc:
            _LOG.info(
                "assistant model round %d failed: %s", round_idx, type(exc).__name__
            )
            return None
        if not isinstance(parsed, dict):
            return None

        reply = parsed.get("reply")
        if isinstance(reply, str) and reply.strip():
            # 日志只记轮数与响应哈希前缀，绝不记原始回复
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
            _LOG.info(
                "assistant model reply: rounds=%d hash=%s", round_idx + 1, digest
            )
            return reply.strip(), settings.assistant_model_name

        tool_call = parsed.get("tool_call")
        if not isinstance(tool_call, dict):
            return None
        name = tool_call.get("name")
        arguments = tool_call.get("arguments")
        if not isinstance(name, str) or not name:
            return None
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return None

        try:
            result = await run_tool(name, session, arguments, via_model=True)
        except AssistantToolError as exc:
            # 工具失败不终止循环：把语义错误回灌给模型，让它自我纠正或改口
            result = {"error": exc.code, "message": str(exc)}
        tool_results.append({"name": name, "result": result})
        blocks.extend(_blocks_from_tool_result(name, result))
        conversation.append(
            {
                "role": "assistant",
                "content": json.dumps(
                    {"tool_call": {"name": name, "arguments": arguments}},
                    ensure_ascii=False,
                ),
            }
        )

    _LOG.info("assistant model exceeded %d rounds, fallback to rules", max_rounds)
    return None


# ----------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------
async def chat(
    session: AsyncSession,
    *,
    user: CurrentUser,
    session_id: str | None,
    text: str | None,
    images: list[bytes] | None,
) -> AssistantReply:
    """处理一轮对话：落库用户消息 → （图片检测）→ 模型/规则 → 落库回复。

    事务边界：只 add + flush，commit 由 get_session 依赖在请求成功时统一完成。
    """
    if not settings.assistant_enabled:
        raise AppException(
            code=CHAT_ASSISTANT_DISABLED,
            message="对话助手已被配置关闭",
            http_status=200,
        )

    text = (text or "").strip()
    images = list(images or [])
    if not text and not images:
        raise AppException(
            code=ErrorCode.PARAM_INVALID,
            message="消息内容为空（需要文字或图片）",
            http_status=200,
        )

    chat_session = await _get_or_create_session(
        session,
        user=user,
        session_id=session_id,
        text=text,
        has_images=bool(images),
    )

    # 落库 user 消息（图片消息 content 为代表文本，图片二进制不入库）
    session.add(
        ChatMessage(
            message_id=_new_message_id(),
            session_id=chat_session.session_id,
            role=ChatRole.USER,
            content=text or "[图片]",
            content_type="image" if images else "text",
            payload={"image_count": len(images)} if images else None,
        )
    )

    # 图片必跑 image.analyze（引擎直调，不经模型 —— 检测必须发生）
    blocks: list[dict[str, Any]] = []
    det_summaries: list[str] = []
    for idx, image_bytes in enumerate(images):
        try:
            result = await run_tool(
                "image.analyze", session, {"image_bytes": image_bytes}
            )
        except AssistantToolError as exc:
            blocks.append(
                {
                    "type": "error",
                    "data": {"message": f"第 {idx + 1} 张图片分析失败：{exc}"},
                }
            )
            continue
        blocks.append({"type": "detection", "data": {"image_index": idx, **result}})
        det_summaries.append(_detection_summary(idx, result))

    # 生成回复：模型路径优先，任何失败回退规则（绝不假装有模型）
    reply_text: str | None = None
    model_used = "rule-fallback"
    fallback = True
    if _model_configured():
        pre_model_blocks = len(blocks)
        try:
            model_result = await _chat_with_model(
                session,
                chat_session=chat_session,
                text=text,
                det_summaries=det_summaries,
                blocks=blocks,
            )
        except Exception as exc:  # noqa: BLE001 —— 模型路径绝不能打穿对话
            _LOG.warning("assistant model path crashed: %s", type(exc).__name__)
            model_result = None
        if model_result is not None:
            reply_text, model_used = model_result
            fallback = False
        else:
            # 回退时剪掉模型路径已追加的工具块，避免规则路径重复出卡
            del blocks[pre_model_blocks:]

    if reply_text is None:
        reply_text = await _rule_path(
            session,
            text=text,
            has_images=bool(images),
            det_summaries=det_summaries,
            blocks=blocks,
        )

    # 落库 assistant 消息（payload 只存结构化展示块，不含思维链/原始 prompt）
    assistant_msg = ChatMessage(
        message_id=_new_message_id(),
        session_id=chat_session.session_id,
        role=ChatRole.ASSISTANT,
        content=reply_text,
        content_type=blocks[0]["type"] if blocks else "text",
        payload={"blocks": blocks} if blocks else None,
    )
    session.add(assistant_msg)
    # 显式触更新会话 updated_at（会话列表按它排序）；onupdate 只在行变脏时触发
    chat_session.updated_at = datetime.now(timezone.utc)
    await session.flush()

    return AssistantReply(
        message_id=assistant_msg.message_id,
        session_id=chat_session.session_id,
        text=reply_text,
        blocks=blocks,
        model_used=model_used,
        fallback=fallback,
    )
