"""对话助手接口 —— 自由对话问答 + 图片拖入即分析。

与「事件处置 Agent」（/agents，派单运行状态机，10 端点冻结契约）**并行**：
这里是对话框用途 —— 打字问答 / 拖图检测 / 派单建议卡。
挂独立前缀 /assistant，不碰 /agents 前缀的冻结断言。

- 全部走 ApiResponse 信封（HTTP 恒 200，code===0 成功）
- 对话工具只读，operator/viewer/匿名均可对话；派单确认仍走既有
  POST /agents/runs（含审批、审计），不在本模块写业务库
- 错误码启用 8xxx 新段（不碰 6xxx Agent / 7xxx 知识两个冻结段）；
  图片解码/推理基础设施不可用复用 5xxx 段的 5001
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import CurrentUser, get_current_user
from app.core.exceptions import ApiResponse, AppException, ErrorCode
from app.db.session import get_session
from app.models.chat import ChatMessage, ChatSession
from app.schemas.chat import (
    AssistantReplyOut,
    ChatBlock,
    ChatMessageOut,
    ChatSessionCreate,
    ChatSessionOut,
)
from app.services.assistant import (
    CHAT_IMAGE_INVALID,
    CHAT_SESSION_NOT_FOUND,
)
from app.services.assistant.engine import chat as run_chat
from app.services.assistant.engine import new_session_id

router = APIRouter()

# 单条消息最多携带图片数（界定了 multipart 请求体的最大体积）
_MAX_IMAGES_PER_MESSAGE = 4


def _chat_image_invalid(message: str) -> AppException:
    return AppException(code=CHAT_IMAGE_INVALID, message=message, http_status=200)


async def _read_and_validate_images(images: list[UploadFile] | None) -> list[bytes]:
    """读取并校验上传图片：空文件 / 超限 / 无法解码 → 8003。

    cv2 可解码性在这里前置检查（而非等引擎产 error block）：
    用户传了坏图就该当场得到明确错误，而不是一条「对话式」的含糊提示。
    """
    if not images:
        return []
    if len(images) > _MAX_IMAGES_PER_MESSAGE:
        raise AppException(
            code=ErrorCode.PARAM_INVALID,
            message=f"单条消息最多 {_MAX_IMAGES_PER_MESSAGE} 张图片",
            http_status=200,
        )

    # cv2/numpy 只在真有图片时才 import，避免缺依赖影响整个 API 模块导入
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise AppException(
            code=ErrorCode.AI_SERVICE_UNAVAILABLE,
            message="图片检测组件不可用（缺少 cv2/numpy 依赖）",
            http_status=200,
        ) from exc

    max_bytes = settings.assistant_max_image_mb * 1024 * 1024
    out: list[bytes] = []
    for idx, image in enumerate(images):
        data = await image.read()
        if not data:
            raise _chat_image_invalid(f"第 {idx + 1} 张图片内容为空")
        if len(data) > max_bytes:
            raise _chat_image_invalid(
                f"第 {idx + 1} 张图片超过 {settings.assistant_max_image_mb}MB 上限"
            )
        nparr = np.frombuffer(data, np.uint8)
        if cv2.imdecode(nparr, cv2.IMREAD_COLOR) is None:
            raise _chat_image_invalid(
                f"第 {idx + 1} 张图片无法解析（需要 jpg/png 格式）"
            )
        out.append(data)
    return out


async def _get_owned_session(
    session: AsyncSession, session_id: str, user: CurrentUser
) -> ChatSession:
    """按编号取会话并校验属主；不存在或不属于当前用户都报 8001（不泄露存在性）。"""
    row = (
        await session.execute(
            select(ChatSession).where(ChatSession.session_id == session_id)
        )
    ).scalar_one_or_none()
    if row is None or row.username != user.username:
        raise AppException(
            code=CHAT_SESSION_NOT_FOUND, message="会话不存在", http_status=200
        )
    return row


@router.post(
    "/chat",
    response_model=ApiResponse[AssistantReplyOut],
    summary="对话（文字 + 可选图片，multipart/form-data）",
)
async def post_chat(
    text: str | None = Form(default=None),
    session_id: str | None = Form(default=None),
    images: list[UploadFile] | None = File(default=None),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """一轮对话：文字 + 0..n 张图片。

    有图片必做 OpenCV 检测（引擎直调）；配齐模型三要素走真实 LLM 工具编排，
    否则规则兜底（响应里 fallback/model_used 如实标明，绝不假装有模型）。
    """
    image_payloads = await _read_and_validate_images(images)
    reply = await run_chat(
        session,
        user=user,
        session_id=session_id or None,
        text=text,
        images=image_payloads,
    )
    return ApiResponse.ok(
        AssistantReplyOut(
            message_id=reply.message_id,
            session_id=reply.session_id,
            text=reply.text,
            blocks=[ChatBlock(**block) for block in reply.blocks],
            model_used=reply.model_used,
            fallback=reply.fallback,
        )
    )


@router.get(
    "/sessions",
    response_model=ApiResponse[list[ChatSessionOut]],
    summary="当前用户的会话列表（按最近更新排序）",
)
async def list_sessions(
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    rows = (
        (
            await session.execute(
                select(ChatSession)
                .where(ChatSession.username == user.username)
                .order_by(ChatSession.updated_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    return ApiResponse.ok([ChatSessionOut.model_validate(row) for row in rows])


@router.post(
    "/sessions",
    response_model=ApiResponse[ChatSessionOut],
    summary="新建空会话",
)
async def create_session(
    payload: ChatSessionCreate,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    chat_session = ChatSession(
        session_id=new_session_id(),
        username=user.username,
        title=(payload.title or "").strip()[:200] or "新对话",
    )
    session.add(chat_session)
    await session.flush()
    await session.refresh(chat_session)
    return ApiResponse.ok(ChatSessionOut.model_validate(chat_session))


@router.get(
    "/sessions/{session_id}/messages",
    response_model=ApiResponse[dict],
    summary="会话历史消息（按时间正序分页，仅属主）",
)
async def list_messages(
    session_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    await _get_owned_session(session, session_id, user)
    total = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ChatMessage)
                .where(ChatMessage.session_id == session_id)
            )
        ).scalar_one()
    )
    rows = (
        (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return ApiResponse.ok(
        {
            "items": [
                ChatMessageOut.model_validate(row).model_dump(mode="json")
                for row in rows
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        }
    )


@router.delete(
    "/sessions/{session_id}",
    response_model=ApiResponse[dict],
    summary="删除会话（级联删消息，仅属主）",
)
async def delete_session(
    session_id: str,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    chat_session = await _get_owned_session(session, session_id, user)
    # 显式删消息再删会话：FK 虽有 ON DELETE CASCADE，但 SQLite 测试库默认
    # 不强制外键，且 ORM 未声明 relationship —— 显式删除保证两种库行为一致。
    await session.execute(
        delete(ChatMessage).where(ChatMessage.session_id == session_id)
    )
    await session.delete(chat_session)
    return ApiResponse.ok({"deleted": True, "session_id": session_id})
