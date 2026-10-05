"""WP-14D ACK 审计查询接口测试 —— GET /api/v1/tasks/{task_id}/acks。

权限矩阵（WP-14D §4）：
- 匿名（无有效令牌）→ 401（code=1003），即使任务存在；
- operator 只能查询本辖区任务：越辖区 → 403（code=1004）；
- admin / 已登录 viewer 可读；
- 任务不存在 → 404（code=4001）；
- 分页 `page` / `page_size` 参数透传给仓储，默认按 received_wall_at DESC；
- 响应只含冻结 11 字段，**不回传** raw_payload / last_payload 隐私原文；
- 使用 ApiResponse 信封（code/message/data/trace_id）。

测试不依赖 PostgreSQL：Session 用桩对象，TaskRepository /
TaskAckRepository 用 monkeypatch 注入假实现（与 test_agent_api 同模式）。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.api.v1 import tasks as tasks_api  # noqa: E402
from app.api.v1.auth import _issue_token  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.middleware.response import register_exception_handlers  # noqa: E402

API = "/api/v1/tasks"

CONTRACT_FIELDS = {
    "command_id", "task_id", "device_id", "seq", "outcome", "accepted",
    "reason", "mode", "received_at", "received_wall_at", "duplicate_count",
}
PRIVATE_FIELDS = {"raw_payload", "last_payload"}


class FakeTask:
    """任务替身（只含本接口需要的字段）。"""

    def __init__(self, task_id: str, township: str | None) -> None:
        self.task_id = task_id
        self.township = township


class FakeAck:
    """ACK 审计行替身（TaskAckOut 用 from_attributes 读取）。"""

    def __init__(
        self,
        command_id: str,
        received_wall_at: datetime,
        *,
        task_id: str = "tsk_demo_001",
        device_id: str = "RBT-DEMO-01",
        seq: int = 1,
        outcome: str = "new",
        accepted: bool = True,
        reason: str | None = None,
        mode: str | None = None,
        duplicate_count: int = 0,
    ) -> None:
        self.command_id = command_id
        self.task_id = task_id
        self.device_id = device_id
        self.seq = seq
        self.outcome = outcome
        self.accepted = accepted
        self.reason = reason
        self.mode = mode
        self.received_at = datetime(2026, 9, 19, 7, 59, 30, tzinfo=timezone.utc)
        self.received_wall_at = received_wall_at
        self.duplicate_count = duplicate_count


def _headers(username: str, role: str, scope: str | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {_issue_token(username, role, scope)}"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """组装 tasks 路由的测试应用：注入假仓储，认证走真实令牌。

    任务 `tsk_demo_001` 属「马鼻镇」，账本 3 行（received_wall_at 降序
    已就绪）。用闭包捕获分页参数，供断言「透传」。
    """
    test_app = FastAPI()
    register_exception_handlers(test_app)
    test_app.include_router(tasks_api.router, prefix=API)

    task = FakeTask("tsk_demo_001", township="马鼻镇")
    acks = [
        FakeAck("cmd_demo_003", datetime(2026, 9, 19, 8, 2, 0, tzinfo=timezone.utc),
                outcome="new", accepted=True, reason="dispatched", mode="navigating"),
        FakeAck("cmd_demo_002", datetime(2026, 9, 19, 8, 1, 0, tzinfo=timezone.utc),
                outcome="late", accepted=True, duplicate_count=2),
        FakeAck("cmd_demo_001", datetime(2026, 9, 19, 8, 0, 0, tzinfo=timezone.utc),
                outcome="new", accepted=False, reason="busy"),
    ]
    captured: dict = {}

    class _FakeTaskRepository:
        def __init__(self, session) -> None:
            self.session = session

        async def get_by_task_id(self, task_id: str) -> FakeTask | None:
            return task if task_id == task.task_id else None

    class _FakeAckRepository:
        def __init__(self, session) -> None:
            self.session = session

        async def list_by_task(self, task_id: str, *, page: int = 1, page_size: int = 20):
            captured["task_id"] = task_id
            captured["page"] = page
            captured["page_size"] = page_size
            start = (page - 1) * page_size
            return acks[start:start + page_size], len(acks)

    # tasks.py 模块顶层绑定名 TaskRepository；端点函数内 from app.repositories
    # import TaskAckRepository → 补丁 app.repositories.TaskAckRepository 生效。
    monkeypatch.setattr(tasks_api, "TaskRepository", _FakeTaskRepository)
    monkeypatch.setattr("app.repositories.TaskAckRepository", _FakeAckRepository)

    async def _session_override():
        yield object()

    test_app.dependency_overrides[get_session] = _session_override

    with TestClient(test_app) as test_client:
        yield test_client, captured, task, acks


class TestPermissionMatrix:
    def test_anonymous_rejected_401(self, client) -> None:
        """★ 仅登录用户可读：匿名即使任务存在也被拒绝。"""
        c, _, _, _ = client
        resp = c.get(f"{API}/tsk_demo_001/acks")
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == 1003
        assert "登录" in body["message"]

    def test_operator_in_scope_readable(self, client) -> None:
        """operator 本辖区任务可读。"""
        c, _, _, _ = client
        resp = c.get(
            f"{API}/tsk_demo_001/acks", headers=_headers("op_01", "operator", "马鼻镇")
        )
        assert resp.status_code == 200
        assert resp.json()["code"] == 0

    def test_operator_out_of_scope_rejected_403(self, client) -> None:
        """★ operator 只能查询本辖区任务：越辖区 403。"""
        c, _, _, _ = client
        resp = c.get(
            f"{API}/tsk_demo_001/acks", headers=_headers("op_02", "operator", "黄岐镇")
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == 1004
        assert "辖区" in resp.json()["message"]

    def test_admin_readable(self, client) -> None:
        c, _, _, _ = client
        resp = c.get(f"{API}/tsk_demo_001/acks", headers=_headers("adm", "admin"))
        assert resp.status_code == 200
        assert resp.json()["code"] == 0

    def test_logged_in_viewer_readable(self, client) -> None:
        """已登录 viewer（只读角色）可读 ACK 审计。"""
        c, _, _, _ = client
        resp = c.get(f"{API}/tsk_demo_001/acks", headers=_headers("vw", "viewer"))
        assert resp.status_code == 200
        assert resp.json()["code"] == 0

    def test_unknown_task_404(self, client) -> None:
        c, _, _, _ = client
        resp = c.get(f"{API}/tsk_missing/acks", headers=_headers("adm", "admin"))
        assert resp.status_code == 404
        assert resp.json()["code"] == 4001


class TestResponseContract:
    def test_envelope_and_frozen_fields_no_privacy_leak(self, client) -> None:
        """ApiResponse 信封 + 冻结 11 字段；不回传 raw_payload/last_payload。"""
        c, _, _, _ = client
        body = c.get(
            f"{API}/tsk_demo_001/acks", headers=_headers("adm", "admin")
        ).json()
        assert body["code"] == 0
        assert {"code", "message", "data", "trace_id"} <= set(body.keys())
        data = body["data"]
        assert set(data.keys()) == {"items", "meta"}
        assert data["meta"] == {"total": 3, "page": 1, "page_size": 20}

        items = data["items"]
        assert len(items) == 3
        # 冻结字段全量存在
        for item in items:
            assert CONTRACT_FIELDS <= set(item.keys()), item
            # 隐私原文绝不出现在响应里
            assert PRIVATE_FIELDS.isdisjoint(item.keys())
        # 判定语义透传
        assert items[0]["command_id"] == "cmd_demo_003"
        assert items[0]["outcome"] == "new"
        assert items[0]["accepted"] is True
        assert items[0]["duplicate_count"] == 0
        assert items[1]["duplicate_count"] == 2
        assert items[2]["accepted"] is False
        assert items[2]["reason"] == "busy"
        # 时间线字段为可序列化的 ISO 字符串
        assert items[0]["received_wall_at"].startswith("2026-09-19T08:02:00")

    def test_default_order_desc_and_pagination_passthrough(self, client) -> None:
        """分页参数透传给仓储；仓储负责 DESC（接口层不重排）。"""
        c, captured, _, _ = client
        resp = c.get(
            f"{API}/tsk_demo_001/acks",
            params={"page": 2, "page_size": 1},
            headers=_headers("adm", "admin"),
        )
        body = resp.json()
        assert body["code"] == 0
        assert captured["task_id"] == "tsk_demo_001"
        assert captured["page"] == 2
        assert captured["page_size"] == 1
        assert body["data"]["meta"] == {"total": 3, "page": 2, "page_size": 1}
        assert len(body["data"]["items"]) == 1
        # 假仓储按 acks 顺序切片：page=2 & size=1 → 第 2 条（cmd_demo_002）
        assert body["data"]["items"][0]["command_id"] == "cmd_demo_002"


class TestOpenApiContract:
    @staticmethod
    def _resolve_props(openapi: dict, node: dict) -> dict:
        """解析 $ref 链，返回最终 schema 的 properties。"""
        for _ in range(10):
            ref = node.get("$ref")
            if ref is None:
                break
            node = openapi["components"]["schemas"][ref.rsplit("/", 1)[1]]
        return node.get("properties", {})

    def test_acks_endpoint_registered_with_envelope(self) -> None:
        """端点挂载在真实应用上，且声明 ApiResponse 信封响应模型。"""
        from app.main import app as real_app

        openapi = real_app.openapi()
        path = f"{API}/{{task_id}}/acks"
        assert path in openapi["paths"], f"缺少端点 {path}"
        op = openapi["paths"][path]["get"]
        assert "summary" in op
        node = op["responses"]["200"]["content"]["application/json"]["schema"]
        props = self._resolve_props(openapi, node)
        assert {"code", "message", "data", "trace_id"} <= set(props), (
            "ACK 查询响应不是 ApiResponse 信封"
        )
