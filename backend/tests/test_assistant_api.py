"""对话助手（/assistant）HTTP 契约与规则兜底测试。

与「事件处置 Agent」（test_agent_api.py，6xxx 段冻结契约）平行：
这里守的是对话框用途的契约 —— 信封结构、规则兜底五分支、会话 CRUD、
属主隔离、8xxx 新段不撞通用段。

不依赖真实 PostgreSQL / cv2：
- 会话/消息两表建在 SQLite 内存库（PostGIS 业务表不建 —— 工具查询经
  ``engine.run_tool`` 替换为假实现，对话编排逻辑与业务库查询解耦，
  后者已由 events/tasks 等自有测试覆盖）。
- 本机无 cv2 时，图片解码校验走 ImportError → 5001 分支；有 cv2 时
  走真实解码 → 检测块。两条路径都断言，各自符合「不假装可用」纪律。

运行：
    pytest tests/test_assistant_api.py -v
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.v1.assistant import router
from app.core.deps import CurrentUser, get_current_user
from app.core.exceptions import ErrorCode
from app.db.session import Base, get_session
from app.middleware.response import register_exception_handlers
from app.models.chat import ChatMessage, ChatSession
from app.services import assistant as assistant_errors

try:  # 本机未必装 cv2 —— 图片解码路径按可用性分断言
    import cv2  # noqa: F401
    import numpy as np

    HAS_CV2 = True
except ImportError:  # pragma: no cover - 取决于运行环境
    HAS_CV2 = False


CHAT_TABLES = [ChatSession.__table__, ChatMessage.__table__]


# ----------------------------------------------------------------------
# 假工具结果（不建业务库表；键名与 app.services.assistant.tools 真实现对齐）
# ----------------------------------------------------------------------
def _fake_tool_result(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "image.analyze":
        return {
            "detections": [
                {"class": "plastic", "confidence": 0.87, "bbox": [1, 2, 30, 40]}
            ],
            "count": 1,
            "width": 64,
            "height": 64,
            "classes": ["foam", "plastic", "fishing_gear", "other"],
            "engine": "opencv",
        }
    if name == "event.list":
        return {
            "items": [
                {
                    "event_id": "evt_20260922_aaa111",
                    "main_class": "plastic",
                    "main_class_label": "塑胶类",
                    "status": "new",
                    "status_label": "待处理",
                },
                {
                    "event_id": "evt_20260922_bbb222",
                    "main_class": "foam",
                    "main_class_label": "泡沫类",
                    "status": "resolved",
                    "status_label": "已清理",
                },
            ],
            "total": 2,
            "hours": args.get("hours", 24),
        }
    if name == "event.get":
        return {
            "found": True,
            "event": {
                "event_id": args.get("event_id"),
                "main_class": "plastic",
                "main_class_label": "塑胶类",
                "status": "new",
                "status_label": "待处理",
                "event_time": "2026-09-22T01:02:03",
                "lng": 119.9,
                "lat": 26.1,
            },
        }
    if name == "stats.overview":
        return {
            "event_count_24h": 7,
            "tasks_by_status": {"pending": 2, "assigned": 1, "done": 5},
            "done_tasks_24h": 3,
            "collected_kg_total": 12.5,
            "devices_by_status": {"online": 4, "offline": 1},
        }
    if name == "dispatch.suggest":
        return {
            "event_id": args.get("event_id"),
            "found": True,
            "event": {"event_id": args.get("event_id"), "main_class": "plastic"},
            "candidates": [
                {"device_id": "robot_01", "name": "海卫一号", "distance_m": 320.0}
            ],
            "hint": "确认派单将创建工单并进入审批流程",
        }
    return {"error": "unknown_tool"}


@pytest.fixture(autouse=True)
def fake_run_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """把引擎内的 run_tool 换成假实现（引擎是 ``from ... import run_tool``，

    故替换点在 engine 模块命名空间）。所有走 /chat 的测试因此不碰业务库。
    """
    import app.services.assistant.engine as engine_mod

    async def _fake(
        name: str,
        session: AsyncSession,
        args: dict[str, Any],
        *,
        via_model: bool = False,
    ) -> dict[str, Any]:
        return _fake_tool_result(name, args)

    monkeypatch.setattr(engine_mod, "run_tool", _fake)


@pytest_asyncio.fixture
async def assistant_api() -> AsyncGenerator[
    tuple[AsyncClient, dict[str, Any]],
    None,
]:
    """SQLite 内存库 + 依赖覆盖的测试客户端（模式同 test_knowledge_api.py）。"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=CHAT_TABLES,
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router, prefix="/api/v1/assistant")
    identity: dict[str, Any] = {
        "username": "viewer",
        "role": "viewer",
        "township_scope": None,
    }

    async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        username=identity["username"],
        role=identity["role"],
        township_scope=identity["township_scope"],
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client, identity

    app.dependency_overrides.clear()
    await engine.dispose()


def act_as(
    identity: dict[str, Any],
    *,
    username: str,
    role: str,
    township_scope: str | None = None,
) -> None:
    identity.update(
        username=username,
        role=role,
        township_scope=township_scope,
    )


def _png_bytes() -> bytes:
    """生成一张可解码的 PNG（无 cv2 时退回一个最小合法 PNG 头字节串）。"""
    if HAS_CV2:
        img = np.zeros((16, 16, 3), dtype=np.uint8)
        ok, buf = cv2.imencode(".png", img)
        assert ok
        return buf.tobytes()
    # 1x1 透明 PNG 的固定字节（无 cv2 环境仅用于触发「需 cv2」分支）
    return bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000d49444154789c626001000000ffff030000060005"
        "57bfabd40000000049454e44ae426082"
    )


def _img_files(count: int, payload: bytes) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [
        ("images", (f"img{i}.png", payload, "image/png")) for i in range(count)
    ]


async def _create_session(client: AsyncClient, title: str = "测试会话") -> str:
    resp = await client.post("/api/v1/assistant/sessions", json={"title": title})
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    return body["data"]["session_id"]


# ----------------------------------------------------------------------
# 信封结构 + 空消息
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chat_envelope_structure(
    assistant_api: tuple[AsyncClient, dict[str, Any]],
) -> None:
    """成功响应必须是 ApiResponse 信封：code===0 + data 含回复四要素。"""
    client, _ = assistant_api
    resp = await client.post("/api/v1/assistant/chat", data={"text": "你好"})
    body = resp.json()
    assert resp.status_code == 200
    assert set(body) >= {"code", "message", "data"}
    assert body["code"] == 0
    data = body["data"]
    assert data["message_id"].startswith("cm_")
    assert data["session_id"].startswith("cs_")
    assert isinstance(data["text"], str) and data["text"]
    assert isinstance(data["blocks"], list)
    # 未配模型 → 必须如实标明规则兜底，绝不假装有模型
    assert data["fallback"] is True
    assert data["model_used"] == "rule-fallback"


@pytest.mark.asyncio
async def test_chat_empty_message_rejected(
    assistant_api: tuple[AsyncClient, dict[str, Any]],
) -> None:
    """既无文字也无图片 → PARAM_INVALID，不能建出空会话/空消息。"""
    client, _ = assistant_api
    resp = await client.post("/api/v1/assistant/chat", data={})
    body = resp.json()
    assert resp.status_code == 200
    assert body["code"] == ErrorCode.PARAM_INVALID


# ----------------------------------------------------------------------
# 规则兜底五分支
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rule_stats_branch(
    assistant_api: tuple[AsyncClient, dict[str, Any]],
) -> None:
    client, _ = assistant_api
    resp = await client.post(
        "/api/v1/assistant/chat", data={"text": "今天有多少起事件？"}
    )
    data = resp.json()["data"]
    assert data["fallback"] is True
    assert any(b["type"] == "stats" for b in data["blocks"])
    assert "24 小时新增事件 7 起" in data["text"]


@pytest.mark.asyncio
async def test_rule_dispatch_branch(
    assistant_api: tuple[AsyncClient, dict[str, Any]],
) -> None:
    client, _ = assistant_api
    resp = await client.post(
        "/api/v1/assistant/chat", data={"text": "evt_20260922_aaa111 派单"}
    )
    data = resp.json()["data"]
    assert any(b["type"] == "dispatch_suggest" for b in data["blocks"])
    # 派单只生成建议卡，须引导走既有 /agents/runs 审批流
    assert "确认派单" in data["text"]


@pytest.mark.asyncio
async def test_rule_event_get_branch(
    assistant_api: tuple[AsyncClient, dict[str, Any]],
) -> None:
    """裸事件编号（无派单/统计/查询词）→ 单事件卡。"""
    client, _ = assistant_api
    resp = await client.post(
        "/api/v1/assistant/chat", data={"text": "evt_20260922_aaa111"}
    )
    data = resp.json()["data"]
    assert any(b["type"] == "event_card" for b in data["blocks"])
    assert "evt_20260922_aaa111" in data["text"]


@pytest.mark.asyncio
async def test_rule_event_list_branch(
    assistant_api: tuple[AsyncClient, dict[str, Any]],
) -> None:
    client, _ = assistant_api
    resp = await client.post(
        "/api/v1/assistant/chat", data={"text": "看看最新事件列表"}
    )
    data = resp.json()["data"]
    assert any(b["type"] == "event_list" for b in data["blocks"])
    assert "最新" in data["text"]


@pytest.mark.asyncio
async def test_rule_capability_branch(
    assistant_api: tuple[AsyncClient, dict[str, Any]],
) -> None:
    """未命中任何意图 → 能力说明，且不该误产任何数据卡。"""
    client, _ = assistant_api
    resp = await client.post("/api/v1/assistant/chat", data={"text": "嗯"})
    data = resp.json()["data"]
    assert data["blocks"] == []
    assert "对话助手" in data["text"]


# ----------------------------------------------------------------------
# 会话 CRUD + 属主隔离 + 级联删除
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chat_auto_creates_session_and_titles_it(
    assistant_api: tuple[AsyncClient, dict[str, Any]],
) -> None:
    """不带 session_id 的对话自动建会话，标题取首条文本前 20 字。"""
    client, _ = assistant_api
    text = "帮我查一下最近的海漂垃圾事件，越详细越好，谢谢"
    resp = await client.post("/api/v1/assistant/chat", data={"text": text})
    session_id = resp.json()["data"]["session_id"]

    listed = await client.get("/api/v1/assistant/sessions")
    items = listed.json()["data"]
    mine = [s for s in items if s["session_id"] == session_id]
    assert mine, "自动建的会话应出现在列表里"
    assert mine[0]["title"] == text[:20]


@pytest.mark.asyncio
async def test_session_crud_and_message_history(
    assistant_api: tuple[AsyncClient, dict[str, Any]],
) -> None:
    client, _ = assistant_api
    session_id = await _create_session(client, "处置讨论")

    # 往该会话发两轮对话
    for text in ("今天有多少事件？", "看看最新事件列表"):
        resp = await client.post(
            "/api/v1/assistant/chat",
            data={"text": text, "session_id": session_id},
        )
        assert resp.json()["data"]["session_id"] == session_id

    # 历史：2 轮 = 2 user + 2 assistant，按时间正序
    history = await client.get(f"/api/v1/assistant/sessions/{session_id}/messages")
    body = history.json()
    assert body["code"] == 0
    assert body["data"]["total"] == 4
    roles = [m["role"] for m in body["data"]["items"]]
    assert roles == ["user", "assistant", "user", "assistant"]

    # 分页：page_size=1 应只回 1 条但 total 仍是 4
    paged = await client.get(
        f"/api/v1/assistant/sessions/{session_id}/messages",
        params={"page": 1, "page_size": 1},
    )
    assert len(paged.json()["data"]["items"]) == 1
    assert paged.json()["data"]["total"] == 4

    # 删除后：会话与消息都应消失
    deleted = await client.delete(f"/api/v1/assistant/sessions/{session_id}")
    assert deleted.json()["code"] == 0
    gone = await client.get(f"/api/v1/assistant/sessions/{session_id}/messages")
    assert gone.json()["code"] == assistant_errors.CHAT_SESSION_NOT_FOUND


@pytest.mark.asyncio
async def test_session_owner_isolation(
    assistant_api: tuple[AsyncClient, dict[str, Any]],
) -> None:
    """他人会话：读消息/删除都报 8001（不泄露存在性），会话列表互不可见。"""
    client, identity = assistant_api
    act_as(identity, username="alice", role="operator")
    session_id = await _create_session(client, "alice 的会话")

    act_as(identity, username="bob", role="operator")
    # bob 看不到 alice 的会话
    listed = await client.get("/api/v1/assistant/sessions")
    assert all(s["session_id"] != session_id for s in listed.json()["data"])
    # bob 读/删 alice 的会话 → 8001（与「不存在」同码，不泄露存在性）
    read = await client.get(f"/api/v1/assistant/sessions/{session_id}/messages")
    assert read.json()["code"] == assistant_errors.CHAT_SESSION_NOT_FOUND
    denied = await client.delete(f"/api/v1/assistant/sessions/{session_id}")
    assert denied.json()["code"] == assistant_errors.CHAT_SESSION_NOT_FOUND
    # bob 也不能往 alice 的会话里发消息
    hijack = await client.post(
        "/api/v1/assistant/chat",
        data={"text": "越权", "session_id": session_id},
    )
    assert hijack.json()["code"] == assistant_errors.CHAT_SESSION_NOT_FOUND


# ----------------------------------------------------------------------
# 权限：viewer / 匿名均可对话（工具全只读）
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_viewer_and_anonymous_can_chat(
    assistant_api: tuple[AsyncClient, dict[str, Any]],
) -> None:
    client, identity = assistant_api
    # 默认 identity 即 viewer
    resp = await client.post("/api/v1/assistant/chat", data={"text": "统计一下"})
    assert resp.json()["code"] == 0

    act_as(identity, username="anonymous", role="viewer")
    resp2 = await client.post("/api/v1/assistant/chat", data={"text": "统计一下"})
    assert resp2.json()["code"] == 0


# ----------------------------------------------------------------------
# 图片：multipart 校验（数量上限 / cv2 可用性 / 检测块）
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chat_rejects_too_many_images(
    assistant_api: tuple[AsyncClient, dict[str, Any]],
) -> None:
    """单条 >4 张图 → PARAM_INVALID（此校验在 cv2 解码之前，与是否有 cv2 无关）。"""
    client, _ = assistant_api
    resp = await client.post(
        "/api/v1/assistant/chat",
        files=_img_files(5, _png_bytes()),
        data={"text": ""},
    )
    assert resp.json()["code"] == ErrorCode.PARAM_INVALID


@pytest.mark.asyncio
async def test_chat_image_decode_path(
    assistant_api: tuple[AsyncClient, dict[str, Any]],
) -> None:
    """有图必做检测：有 cv2 → 检测块；无 cv2 → 5001 如实报组件不可用。"""
    client, _ = assistant_api
    resp = await client.post(
        "/api/v1/assistant/chat",
        files=_img_files(1, _png_bytes()),
        data={"text": ""},
    )
    body = resp.json()
    assert resp.status_code == 200
    if HAS_CV2:
        assert body["code"] == 0
        assert any(b["type"] == "detection" for b in body["data"]["blocks"])
        assert "第1张图" in body["data"]["text"]
    else:
        assert body["code"] == ErrorCode.AI_SERVICE_UNAVAILABLE


# ----------------------------------------------------------------------
# 错误码：8xxx 新段，不撞通用段 / 6xxx / 7xxx
# ----------------------------------------------------------------------
def test_error_codes_live_in_8xxx_segment() -> None:
    """对话助手四码必须在 8000-8999、互不相同、且不占用 ErrorCode 任何值。"""
    codes = [
        assistant_errors.CHAT_SESSION_NOT_FOUND,
        assistant_errors.CHAT_MESSAGE_NOT_FOUND,
        assistant_errors.CHAT_IMAGE_INVALID,
        assistant_errors.CHAT_ASSISTANT_DISABLED,
    ]
    assert all(8000 <= c <= 8999 for c in codes)
    assert len(set(codes)) == len(codes)
    existing = {int(v) for v in vars(ErrorCode).values() if isinstance(v, int)}
    assert not (set(codes) & existing), "8xxx 段与通用 ErrorCode 发生冲突"
