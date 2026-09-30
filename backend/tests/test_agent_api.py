"""WP-03 Agent API 契约与权限测试。

本文件把接口层最容易“看起来能用”但实际不可靠的部分钉死：
- 冻结的 10 个端点与 6xxx 错误码契约（对账守卫）；
- 权限矩阵：viewer 只读 / operator 可写不可审批 / admin+approver 可审批，
  匿名等价 viewer；
- 真实运行状态：无数据时 runtime idle、/ai/agents 返回 idle/unavailable，
  不得写死 running；
- 正常 run 闭环、幂等重放（同 run_id + replay 标记）、分页倒序、
  审批续跑、取消、6001/6002/6003/6004/6006/6007/6008 错误码；
- 写操作审计留痕（复用 services/audit.record_audit）。

测试不依赖 PostgreSQL/PostGIS：Session 用 RecordingSession 桩，
业务快照用 monkeypatch 注入，Runtime 注入内存实例（可注入依赖）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import agents as agents_api
from app.api.v1.auth import _issue_token
from app.core.deps import get_current_user
from app.db.session import get_session
from app.main import app as real_app
from app.middleware.response import register_exception_handlers
from app.models.agent import AgentApproval as AgentApprovalORM
from app.models.agent import AgentRun as AgentRunORM
from app.models.agent import AgentStep as AgentStepORM
from app.models.agent_state import AgentRunState as AgentRunStateORM
from app.models.device import Device
from app.models.event import Event, EventStatus
from app.models.misc import AuditLog
from app.models.task import Task
from app.mqtt import client as mqtt_mod
from app.services.agents import (
    LESSON_MEMORY_TYPE,
    LESSON_SCOPE_TYPE,
    TERMINAL_STATUSES,
    AgentRunRequest,
    AgentRuntime,
    ApprovalRecord,
    FakeClock,
    InMemoryMemoryStore,
    LessonStore,
    RiskLevel,
    RuntimeConfig,
    SequenceIdFactory,
)
from app.ws import manager as ws_mod

API = "/api/v1/agents"


# ----------------------------------------------------------------------
# 异步 Session 桩（只实现 Agent API 用到的表面）
# ----------------------------------------------------------------------


class _ScalarRows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _Result:
    def __init__(self, scalar: Any = None, rows: list[Any] | None = None) -> None:
        self._scalar = scalar
        self._rows = rows or []

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self._scalar

    def scalars(self) -> _ScalarRows:
        return _ScalarRows(self._rows)


class RecordingSession:
    """记录 add / flush / execute，供镜像与审计断言。"""

    def __init__(self) -> None:
        self.added: list[Any] = []
        self.flush_count = 0

    def add(self, item: Any) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        self.flush_count += 1

    async def execute(self, _stmt: Any) -> _Result:
        # 存量运行/步骤/设备镜像查询返回空：内存运行是主数据源。
        return _Result()

    async def commit(self) -> None:
        return None


class DispatchRecorder:
    """记录 Agent 直建单后的 MQTT 下发与 WebSocket 推送。"""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []
        self.pushed: list[dict[str, Any]] = []

    async def publish_task(self, **kwargs: Any) -> bool:
        self.published.append(dict(kwargs))
        return True

    async def push_task_update(self, payload: dict[str, Any]) -> None:
        self.pushed.append(dict(payload))


# ----------------------------------------------------------------------
# 业务快照桩
# ----------------------------------------------------------------------


def _event(
    event_id: str = "evt_wp03",
    *,
    status: str = EventStatus.NEW,
) -> Event:
    return Event(
        event_id=event_id,
        device_id="cam_01",
        event_time=datetime(2026, 9, 19, 10, 0, 0),
        location="SRID=4326;POINT(119.6 26.2)",
        main_class="foam",
        det_count=3,
        max_confidence=Decimal("0.9231"),
        seq=1,
        status=status,
        township="马鼻镇",
    )


def _robot(robot_id: str = "rb_01") -> Device:
    return Device(
        device_id=robot_id,
        device_type="robot",
        name=f"机器人 {robot_id}",
        location="SRID=4326;POINT(119.601 26.201)",
        status="online",
        meta={"battery": 88, "bins": {"foam": 0.1, "plastic": 0.1, "other": 0.1}},
    )


def _snapshot(
    *,
    event_id: str = "evt_wp03",
    event_status: str = EventStatus.NEW,
    conflict_reason: str | None = None,
    merge_task_id: str | None = None,
    with_robot: bool = True,
) -> agents_api.AgentEventSnapshot:
    snapshot = agents_api.AgentEventSnapshot(
        event=_event(event_id, status=event_status),
        lng=119.6,
        lat=26.2,
        conflict_reason=conflict_reason,
        merge_task_id=merge_task_id,
    )
    if with_robot:
        snapshot.candidates = [
            agents_api.RobotSnapshot(
                device=_robot(),
                distance_m=121.5,
                battery=88,
                bin_usage=0.3,
                same_category_active=True,
            )
        ]
    return snapshot


# ----------------------------------------------------------------------
# Fixtures 与辅助
# ----------------------------------------------------------------------


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture(autouse=True)
def dispatch_recorder(monkeypatch: pytest.MonkeyPatch) -> DispatchRecorder:
    recorder = DispatchRecorder()
    monkeypatch.setattr(mqtt_mod.mqtt_client, "publish_task", recorder.publish_task)
    monkeypatch.setattr(ws_mod.ws_manager, "push_task_update", recorder.push_task_update)
    return recorder


@pytest.fixture
def client(session: RecordingSession):
    test_app = FastAPI()
    register_exception_handlers(test_app)
    test_app.include_router(agents_api.router, prefix=API)

    async def _session_override():
        yield session

    test_app.dependency_overrides[get_session] = _session_override
    agents_api.reset_agent_runtime_for_tests()
    with TestClient(test_app) as test_client:
        yield test_client
    agents_api.reset_agent_runtime_for_tests()


def _headers(username: str, role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_issue_token(username, role)}"}


def _patch_snapshot(monkeypatch: pytest.MonkeyPatch, snapshot: agents_api.AgentEventSnapshot):
    async def _load(_session: Any, _event_id: str) -> agents_api.AgentEventSnapshot:
        return snapshot

    monkeypatch.setattr(agents_api, "_load_agent_snapshot", _load)


def _make_runtime(
    *,
    require_approval_risk: tuple[RiskLevel, ...] = (),
    clock: FakeClock | None = None,
    id_factory: Any = None,
) -> AgentRuntime:
    """构造带业务工具集的确定性 Runtime（可注入时钟/ID 工厂）。"""
    runtime = AgentRuntime(
        runtime_config=RuntimeConfig(
            policy_version="rules-work-order-v1.0",
            rule_plan=agents_api.WORK_ORDER_PLAN,
            model_available=False,
            require_approval_risk=require_approval_risk,
        ),
        clock=clock,
        id_factory=id_factory,
    )
    for tool in agents_api._tool_definitions():
        runtime.registry.register(tool)
    return runtime


def _inject_runtime(monkeypatch: pytest.MonkeyPatch, runtime: AgentRuntime) -> None:
    monkeypatch.setattr(agents_api, "_RUNTIME", runtime)


def _audit_actions(session: RecordingSession) -> set[str]:
    return {item.action for item in session.added if isinstance(item, AuditLog)}


# ----------------------------------------------------------------------
# 冻结契约
# ----------------------------------------------------------------------


class TestFrozenContract:
    def test_frozen_endpoints_registered(self) -> None:
        actual = {
            (method, route.path)
            for route in real_app.routes
            for method in getattr(route, "methods", set())
            if route.path.startswith(API)
        }
        expected = {
            ("GET", f"{API}/runtime/status"),
            ("GET", f"{API}/runs"),
            ("POST", f"{API}/runs"),
            ("GET", f"{API}/runs/{{run_id}}"),
            ("GET", f"{API}/runs/{{run_id}}/steps"),
            ("POST", f"{API}/runs/{{run_id}}/cancel"),
            ("GET", f"{API}/tools"),
            ("GET", f"{API}/approvals"),
            ("POST", f"{API}/approvals/{{approval_id}}/decide"),
            ("GET", f"{API}/evals/latest"),
        }
        assert expected <= actual

    def test_agent_error_codes_frozen_6xxx(self) -> None:
        """6xxx 错误码契约（WP-03 设计，穷举并固定）。"""
        assert agents_api.AGENT_RUN_NOT_FOUND == 6001
        assert agents_api.AGENT_ILLEGAL_STATE_TRANSITION == 6002
        assert agents_api.AGENT_APPROVAL_NOT_DECIDABLE == 6003
        assert agents_api.AGENT_RUN_NOT_CANCELLABLE == 6004
        assert agents_api.AGENT_EVENT_NOT_FOUND == 6005
        assert agents_api.AGENT_EVENT_NOT_DISPATCHABLE == 6006
        assert agents_api.AGENT_TASK_CONFLICT == 6007
        assert agents_api.AGENT_EVAL_UNAVAILABLE == 6008
        assert agents_api.AGENT_RUNTIME_UNAVAILABLE == 6009
        codes = {
            agents_api.AGENT_RUN_NOT_FOUND,
            agents_api.AGENT_ILLEGAL_STATE_TRANSITION,
            agents_api.AGENT_APPROVAL_NOT_DECIDABLE,
            agents_api.AGENT_RUN_NOT_CANCELLABLE,
            agents_api.AGENT_EVENT_NOT_FOUND,
            agents_api.AGENT_EVENT_NOT_DISPATCHABLE,
            agents_api.AGENT_TASK_CONFLICT,
            agents_api.AGENT_EVAL_UNAVAILABLE,
            agents_api.AGENT_RUNTIME_UNAVAILABLE,
        }
        assert len(codes) == 9  # 全部唯一


# ----------------------------------------------------------------------
# 真实运行状态
# ----------------------------------------------------------------------


class TestRuntimeStatus:
    def test_runtime_idle_when_no_data(self, client: TestClient) -> None:
        """未初始化 / 无运行数据 → idle，绝不写死 running。"""
        payload = client.get(f"{API}/runtime/status").json()["data"]
        assert payload["state"] == "idle"
        assert payload["active_runs"] == 0
        assert payload["total_runs"] == 0
        assert payload["runtime"] == "in_process"
        assert payload["persistence"] == "mirrored"

    def test_runtime_running_with_active_run(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = _make_runtime(require_approval_risk=(RiskLevel.WRITE,))
        runtime.run(
            AgentRunRequest(
                trigger_type="event",
                objective="处置事件 evt_x",
                actor="operator",
                role="operator",
                params={"event_id": "evt_x"},
            )
        )
        _inject_runtime(monkeypatch, runtime)

        payload = client.get(f"{API}/runtime/status").json()["data"]
        assert payload["state"] == "running"
        assert payload["active_runs"] == 1
        assert payload["pending_approvals"] == 1


class TestRuntimeConfiguration:
    def test_write_approval_flag_is_explicit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(agents_api.settings, "agent_persistent_repository_enabled", False)
        monkeypatch.setattr(agents_api.settings, "agent_require_approval_for_write", False)
        default_runtime = agents_api._build_runtime()
        assert RiskLevel.WRITE not in default_runtime.config.require_approval_risk
        assert RiskLevel.DEVICE_COMMAND in default_runtime.config.require_approval_risk
        assert RiskLevel.SENSITIVE in default_runtime.config.require_approval_risk

        monkeypatch.setattr(agents_api.settings, "agent_require_approval_for_write", True)
        strict_runtime = agents_api._build_runtime()
        assert RiskLevel.WRITE in strict_runtime.config.require_approval_risk

    def test_model_adapter_is_wired_only_when_configured(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(agents_api.settings, "agent_persistent_repository_enabled", False)
        monkeypatch.setattr(agents_api.settings, "agent_model_adapter_enabled", False)
        monkeypatch.setattr(agents_api.settings, "agent_model_base_url", "")
        monkeypatch.setattr(agents_api.settings, "agent_model_api_key", "")
        monkeypatch.setattr(agents_api.settings, "agent_model_name", "")

        default_runtime = agents_api._build_runtime()
        assert default_runtime.model_adapter is None
        assert default_runtime.status().model_available is False
        assert default_runtime.status().rule_mode is True

        monkeypatch.setattr(agents_api.settings, "agent_model_adapter_enabled", True)
        monkeypatch.setattr(
            agents_api.settings,
            "agent_model_base_url",
            "https://model.example/v1",
        )
        monkeypatch.setattr(agents_api.settings, "agent_model_api_key", "sk-test")
        monkeypatch.setattr(agents_api.settings, "agent_model_name", "agent-model")

        model_runtime = agents_api._build_runtime()
        assert model_runtime.model_adapter is not None
        assert model_runtime.model_adapter.enabled is True
        assert isinstance(
            model_runtime.model_adapter.client,
            agents_api.OpenAICompatibleModelClient,
        )
        assert model_runtime.status().model_available is True
        assert model_runtime.status().rule_mode is False

    def test_enabled_but_incomplete_model_config_falls_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(agents_api.settings, "agent_persistent_repository_enabled", False)
        monkeypatch.setattr(agents_api.settings, "agent_model_adapter_enabled", True)
        monkeypatch.setattr(agents_api.settings, "agent_model_base_url", "")
        monkeypatch.setattr(agents_api.settings, "agent_model_name", "")

        runtime = agents_api._build_runtime()
        assert runtime.model_adapter is not None
        assert runtime.model_adapter.enabled is True
        assert runtime.model_adapter.client is None
        assert runtime.status().model_available is False
        assert runtime.status().rule_mode is True


# ----------------------------------------------------------------------
# 权限矩阵
# ----------------------------------------------------------------------


class TestPermissionMatrix:
    def test_viewer_read_only_writes_rejected(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 固定评测产物路径：确定性验证「读端点可达」而不是宽泛 allowlist
        monkeypatch.setattr(
            agents_api,
            "_EVAL_JSON_PATH",
            Path("Z:/definitely/not/exist/agent_evals/latest.json"),
        )
        headers = _headers("viewer", "viewer")
        # 读：全部放行
        assert client.get(f"{API}/runtime/status", headers=headers).json()["code"] == 0
        assert client.get(f"{API}/runs", headers=headers).json()["code"] == 0
        assert client.get(f"{API}/tools", headers=headers).json()["code"] == 0
        assert client.get(f"{API}/approvals", headers=headers).json()["code"] == 0
        # 评测产物缺失 → 6008（确定性断言，不是 0/6008 二选一的 allowlist）
        assert (
            client.get(f"{API}/evals/latest", headers=headers).json()["code"]
            == agents_api.AGENT_EVAL_UNAVAILABLE
        )
        assert client.get(f"{API}/runs/run_missing", headers=headers).json()["code"] == 6001
        assert client.get(f"{API}/runs/run_missing/steps", headers=headers).json()["code"] == 6001
        # 写：一律 403 / 1004
        denied = client.post(
            f"{API}/runs", json={"event_id": "evt_wp03"}, headers=headers
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == 1004
        denied = client.post(
            f"{API}/runs/run_x/cancel", json={"reason": "x"}, headers=headers
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == 1004
        denied = client.post(
            f"{API}/approvals/apr_x/decide",
            json={"decision": "approved"},
            headers=headers,
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == 1004

    def test_anonymous_is_viewer(self, client: TestClient) -> None:
        assert client.get(f"{API}/tools").json()["code"] == 0
        denied = client.post(f"{API}/runs", json={"event_id": "evt_wp03"})
        assert denied.status_code == 403
        assert denied.json()["code"] == 1004

    def test_operator_write_but_cannot_approve(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_snapshot(monkeypatch, _snapshot())
        headers = _headers("operator", "operator")

        started = client.post(
            f"{API}/runs", json={"event_id": "evt_wp03"}, headers=headers
        ).json()
        assert started["code"] == 0
        assert started["data"]["status"] == "succeeded"

        # operator 不可审批（即便审批存在）
        denied = client.post(
            f"{API}/approvals/apr_x/decide",
            json={"decision": "approved"},
            headers=headers,
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == 1004

    def test_approver_role_and_admin_can_decide(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = _make_runtime(require_approval_risk=(RiskLevel.WRITE,))
        _inject_runtime(monkeypatch, runtime)
        _patch_snapshot(monkeypatch, _snapshot())

        waiting = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=_headers("operator", "operator"),
        ).json()
        approval_id = waiting["data"]["pending_approval_ids"][0]

        # approver 角色决定（拒绝）→ run 按 WP-01 语义终止为 failed
        decided = client.post(
            f"{API}/approvals/{approval_id}/decide",
            json={"decision": "rejected", "reason": "本次不派单"},
            headers=_headers("approver_user", "approver"),
        ).json()
        assert decided["code"] == 0
        assert decided["data"]["status"] == "failed"
        assert decided["data"]["error_code"] == "approval_rejected"

        # admin 角色可决定（approver 已拒绝后，另起一单走 admin 通过路径见 TestApproval）


# ----------------------------------------------------------------------
# 启动 run 闭环
# ----------------------------------------------------------------------


class TestStartRun:
    def test_operator_creates_task_and_audits(
        self,
        client: TestClient,
        session: RecordingSession,
        dispatch_recorder: DispatchRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        snapshot = _snapshot()
        _patch_snapshot(monkeypatch, snapshot)

        response = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03", "idempotency_key": "idem-wp03-created"},
            headers=_headers("operator", "operator"),
        )
        body = response.json()
        assert response.status_code == 200
        assert body["code"] == 0
        assert body["data"]["status"] == "succeeded"
        assert body["data"]["task_action"] == "created"
        assert body["data"]["task_id"]
        assert snapshot.event.status == EventStatus.DISPATCHED

        tasks = [item for item in session.added if isinstance(item, Task)]
        assert len(tasks) == 1
        assert tasks[0].event_id == "evt_wp03"
        assert tasks[0].robot_id == "rb_01"

        # 审计 + ORM 镜像（run/step/approval 表不直接暴露，只断言留痕）
        assert _audit_actions(session) >= {"agent_task_create", "agent_run_start"}
        assert any(isinstance(item, AgentRunORM) for item in session.added)
        assert any(isinstance(item, AgentStepORM) for item in session.added)
        assert len(dispatch_recorder.published) == 1
        assert dispatch_recorder.published[0]["task_id"] == tasks[0].task_id
        assert dispatch_recorder.published[0]["robot_id"] == "rb_01"
        assert len(dispatch_recorder.pushed) == 1
        assert dispatch_recorder.pushed[0]["task_id"] == tasks[0].task_id

    def test_idempotent_replay_returns_same_run_with_flag(
        self,
        client: TestClient,
        session: RecordingSession,
        dispatch_recorder: DispatchRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_snapshot(monkeypatch, _snapshot())
        request = {"event_id": "evt_wp03", "idempotency_key": "idem-wp03-replay"}

        first = client.post(
            f"{API}/runs", json=request, headers=_headers("operator", "operator")
        ).json()
        second = client.post(
            f"{API}/runs", json=request, headers=_headers("operator", "operator")
        ).json()

        assert first["data"]["run_id"] == second["data"]["run_id"]
        assert second["data"]["idempotent_replay"] is True
        assert second["message"] == "幂等重放：返回已有运行"
        # 重放不得新建任务
        assert len([item for item in session.added if isinstance(item, Task)]) == 1
        # 重放也不得重复向机器人下发或向看板推送
        assert len(dispatch_recorder.published) == 1
        assert len(dispatch_recorder.pushed) == 1

    def test_merge_reuses_existing_task_and_audits(
        self,
        client: TestClient,
        session: RecordingSession,
        dispatch_recorder: DispatchRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        snapshot = _snapshot(merge_task_id="task_existing")
        _patch_snapshot(monkeypatch, snapshot)

        body = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=_headers("operator", "operator"),
        ).json()

        assert body["code"] == 0
        assert body["data"]["task_action"] == "merged"
        assert body["data"]["task_id"] == "task_existing"
        assert snapshot.event.status == EventStatus.DISPATCHED
        assert not any(isinstance(item, Task) for item in session.added)
        assert "agent_task_merge" in _audit_actions(session)
        # 合并只复用既有任务，不重复下发给机器人
        assert dispatch_recorder.published == []
        assert dispatch_recorder.pushed == []

    def test_no_robot_is_failed_run_not_business_error(
        self,
        client: TestClient,
        session: RecordingSession,
        dispatch_recorder: DispatchRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_snapshot(monkeypatch, _snapshot(with_robot=False))

        response = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=_headers("operator", "operator"),
        )
        body = response.json()

        assert response.status_code == 200
        assert body["code"] == 0
        assert body["data"]["status"] == "failed"
        assert body["data"]["error_code"] == "no_robot_available"
        assert not any(isinstance(item, Task) for item in session.added)
        assert dispatch_recorder.published == []
        assert dispatch_recorder.pushed == []

    def test_event_not_dispatchable_6006(
        self,
        client: TestClient,
        session: RecordingSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_snapshot(
            monkeypatch,
            _snapshot(
                event_status=EventStatus.DISPATCHED,
                conflict_reason="事件当前状态为 dispatched，仅 new 可派单",
            ),
        )

        body = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=_headers("operator", "operator"),
        ).json()

        assert body["code"] == agents_api.AGENT_EVENT_NOT_DISPATCHABLE
        assert session.added == []

    def test_event_not_found_6005(
        self,
        client: TestClient,
        session: RecordingSession,
    ) -> None:
        """不注入快照：真实 _load_agent_snapshot 查不到事件 → 6005。"""
        body = client.post(
            f"{API}/runs",
            json={"event_id": "evt_does_not_exist"},
            headers=_headers("operator", "operator"),
        ).json()
        assert body["code"] == agents_api.AGENT_EVENT_NOT_FOUND
        assert session.added == []

    def test_active_task_conflict_6007(
        self,
        client: TestClient,
        session: RecordingSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_snapshot(
            monkeypatch,
            _snapshot(conflict_reason="该事件已存在活跃工单，拒绝重复建单"),
        )

        body = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=_headers("operator", "operator"),
        ).json()

        assert body["code"] == agents_api.AGENT_TASK_CONFLICT
        assert session.added == []


# ----------------------------------------------------------------------
# 分页与倒序
# ----------------------------------------------------------------------


class TestRunsPagination:
    def test_runs_paginated_desc_by_creation(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = FakeClock(datetime(2026, 1, 1, 8, 0, 0))
        runtime = _make_runtime(clock=clock, id_factory=SequenceIdFactory())
        _inject_runtime(monkeypatch, runtime)
        _patch_snapshot(monkeypatch, _snapshot())
        headers = _headers("operator", "operator")

        run_ids: list[str] = []
        for _ in range(5):
            body = client.post(
                f"{API}/runs",
                json={"event_id": "evt_wp03"},
                headers=headers,
            ).json()
            assert body["code"] == 0
            run_ids.append(body["data"]["run_id"])
            clock.advance(1000)
        # 5 次创建得到 5 个互不相同的 run_id
        assert len(run_ids) == 5
        assert len(set(run_ids)) == 5

        page1 = client.get(
            f"{API}/runs", params={"page": 1, "page_size": 2}, headers=headers
        ).json()
        assert page1["code"] == 0
        data = page1["data"]
        # 按创建时间倒序：第 1 页 = 最新 2 条
        assert [item["run_id"] for item in data["items"]] == list(
            reversed(run_ids)
        )[:2]
        # PageResult 风格元信息（pages 为 PageMeta 计算属性，客户端自行推导）
        assert data["meta"] == {"total": 5, "page": 1, "page_size": 2}
        assert data["meta"]["total"] // data["meta"]["page_size"] + 1 == 3
        created = [item["created_at"] for item in data["items"]]
        assert created[0] > created[1]

        page2 = client.get(
            f"{API}/runs", params={"page": 2, "page_size": 2}, headers=headers
        ).json()
        assert [item["run_id"] for item in page2["data"]["items"]] == list(
            reversed(run_ids)
        )[2:4]
        # 第 2 页元信息正确：total 仍为全集
        assert page2["data"]["meta"]["total"] == 5
        assert page2["data"]["meta"]["page"] == 2

        page3 = client.get(
            f"{API}/runs", params={"page": 3, "page_size": 2}, headers=headers
        ).json()
        assert [item["run_id"] for item in page3["data"]["items"]] == list(
            reversed(run_ids)
        )[4:]
        assert page3["data"]["meta"]["total"] == 5

    def test_runs_default_page_and_status_filter(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clock = FakeClock(datetime(2026, 1, 1, 8, 0, 0))
        runtime = _make_runtime(clock=clock, id_factory=SequenceIdFactory())
        _inject_runtime(monkeypatch, runtime)
        _patch_snapshot(monkeypatch, _snapshot())
        headers = _headers("operator", "operator")

        for _ in range(3):
            client.post(
                f"{API}/runs", json={"event_id": "evt_wp03"}, headers=headers
            )
            clock.advance(1000)

        default = client.get(f"{API}/runs", headers=headers).json()["data"]
        assert default["meta"] == {"total": 3, "page": 1, "page_size": 20}

        filtered = client.get(
            f"{API}/runs", params={"status": "succeeded"}, headers=headers
        ).json()["data"]
        assert filtered["meta"]["total"] == 3

        empty = client.get(
            f"{API}/runs", params={"status": "waiting_approval"}, headers=headers
        ).json()["data"]
        assert empty["items"] == []
        assert empty["meta"]["total"] == 0


# ----------------------------------------------------------------------
# 运行详情 / 状态 / 取消
# ----------------------------------------------------------------------


class TestRunDetailAndCancel:
    def test_orm_replay_restores_task_result_from_state_row(self) -> None:
        now = datetime(2026, 9, 19, 12, 0, 0)
        run = AgentRunORM(
            run_id="run_replay",
            trigger_type="event",
            objective="回放任务结果",
            status="succeeded",
            policy_version="rules-work-order-v1.0",
            trace_id="trace_replay",
            created_at=now,
            updated_at=now,
        )
        state = AgentRunStateORM(
            run_id="run_replay",
            request_json="{}",
            runtime_state_json=json.dumps(
                {
                    "stage": "done",
                    "task_result": {
                        "task_id": "task_replay",
                        "action": "created",
                    },
                }
            ),
            state_version=2,
        )

        detail = agents_api._run_out_from_orm(run, [], state)

        assert detail.task_id == "task_replay"
        assert detail.task_action == "created"

    def test_run_detail_returns_real_status_and_steps(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = _make_runtime(require_approval_risk=(RiskLevel.WRITE,))
        _inject_runtime(monkeypatch, runtime)
        _patch_snapshot(monkeypatch, _snapshot())

        waiting = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=_headers("operator", "operator"),
        ).json()
        run_id = waiting["data"]["run_id"]
        assert waiting["data"]["status"] == "waiting_approval"
        assert waiting["data"]["pending_approval_ids"]

        detail = client.get(f"{API}/runs/{run_id}").json()
        assert detail["code"] == 0
        assert detail["data"]["status"] == "waiting_approval"
        step_types = [step["step_type"] for step in detail["data"]["steps"]]
        assert "approval_request" in step_types

        # 状态过滤：非终态 run 可被 waiting_approval 查到
        page = client.get(f"{API}/runs", params={"status": "waiting_approval"}).json()
        assert page["data"]["meta"]["total"] == 1

    def test_unknown_run_6001(self, client: TestClient) -> None:
        assert client.get(f"{API}/runs/run_missing").json()["code"] == 6001
        assert client.get(f"{API}/runs/run_missing/steps").json()["code"] == 6001
        denied = client.post(
            f"{API}/runs/run_missing/cancel",
            json={"reason": "x"},
            headers=_headers("operator", "operator"),
        ).json()
        assert denied["code"] == 6001

    def test_cancel_active_run(
        self,
        client: TestClient,
        session: RecordingSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = _make_runtime(require_approval_risk=(RiskLevel.WRITE,))
        _inject_runtime(monkeypatch, runtime)
        _patch_snapshot(monkeypatch, _snapshot())

        waiting = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=_headers("operator", "operator"),
        ).json()
        run_id = waiting["data"]["run_id"]

        cancelled = client.post(
            f"{API}/runs/{run_id}/cancel",
            json={"reason": "人工介入，改走线下"},
            headers=_headers("operator", "operator"),
        ).json()
        assert cancelled["code"] == 0
        assert cancelled["data"]["status"] == "cancelled"
        assert cancelled["data"]["status"] in TERMINAL_STATUSES
        assert "agent_run_cancel" in _audit_actions(session)

    def test_cancel_terminal_run_6004(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_snapshot(monkeypatch, _snapshot())
        started = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=_headers("operator", "operator"),
        ).json()
        assert started["data"]["status"] == "succeeded"

        body = client.post(
            f"{API}/runs/{started['data']['run_id']}/cancel",
            json={"reason": "重复取消"},
            headers=_headers("operator", "operator"),
        ).json()
        assert body["code"] == agents_api.AGENT_RUN_NOT_CANCELLABLE


# ----------------------------------------------------------------------
# 审批
# ----------------------------------------------------------------------


class TestApproval:
    def test_approval_flow_resumes_after_admin_decision(
        self,
        client: TestClient,
        session: RecordingSession,
        dispatch_recorder: DispatchRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = _make_runtime(require_approval_risk=(RiskLevel.WRITE,))
        _inject_runtime(monkeypatch, runtime)
        snapshot = _snapshot()
        _patch_snapshot(monkeypatch, snapshot)

        waiting = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=_headers("operator", "operator"),
        ).json()
        assert waiting["data"]["status"] == "waiting_approval"
        approval_id = waiting["data"]["pending_approval_ids"][0]
        assert any(isinstance(item, AgentApprovalORM) for item in session.added)

        denied = client.post(
            f"{API}/approvals/{approval_id}/decide",
            json={"decision": "approved"},
            headers=_headers("operator", "operator"),
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == 1004

        approved = client.post(
            f"{API}/approvals/{approval_id}/decide",
            json={"decision": "approved", "reason": "管理员确认"},
            headers=_headers("admin", "admin"),
        ).json()
        assert approved["code"] == 0
        assert approved["data"]["status"] == "succeeded"
        assert approved["data"]["task_action"] == "created"
        assert snapshot.event.status == EventStatus.DISPATCHED
        assert "agent_approval_decide" in _audit_actions(session)
        assert len(dispatch_recorder.published) == 1
        assert len(dispatch_recorder.pushed) == 1

    def test_unknown_approval_6003(self, client: TestClient) -> None:
        body = client.post(
            f"{API}/approvals/apr_missing/decide",
            json={"decision": "approved"},
            headers=_headers("admin", "admin"),
        ).json()
        assert body["code"] == agents_api.AGENT_APPROVAL_NOT_DECIDABLE

    def test_already_decided_6003(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = _make_runtime(require_approval_risk=(RiskLevel.WRITE,))
        _inject_runtime(monkeypatch, runtime)
        _patch_snapshot(monkeypatch, _snapshot())

        waiting = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=_headers("operator", "operator"),
        ).json()
        approval_id = waiting["data"]["pending_approval_ids"][0]
        admin = _headers("admin", "admin")
        assert (
            client.post(
                f"{API}/approvals/{approval_id}/decide",
                json={"decision": "approved"},
                headers=admin,
            ).json()["code"]
            == 0
        )
        # 重复决定 → 6003（不可决定）
        second = client.post(
            f"{API}/approvals/{approval_id}/decide",
            json={"decision": "rejected"},
            headers=admin,
        ).json()
        assert second["code"] == agents_api.AGENT_APPROVAL_NOT_DECIDABLE

    def test_decide_on_terminal_run_6002(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """审批仍挂起但 run 已终态 → 非法状态迁移 6002（状态一致性守卫）。"""
        runtime = _make_runtime()
        _inject_runtime(monkeypatch, runtime)
        _patch_snapshot(monkeypatch, _snapshot())

        started = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=_headers("operator", "operator"),
        ).json()
        run_id = started["data"]["run_id"]
        assert started["data"]["status"] == "succeeded"

        # 直接构造一条仍挂起的审批记录，指向已终态 run（正常流程不可达，
        # 用于钉死接口的状态一致性守卫）。
        runtime.approvals.create(
            ApprovalRecord(
                approval_id="apr_orphan",
                run_id=run_id,
                requested_action="task.create_or_merge",
                risk_level=RiskLevel.WRITE.value,
                requested_by="operator",
            )
        )
        body = client.post(
            f"{API}/approvals/apr_orphan/decide",
            json={"decision": "approved"},
            headers=_headers("admin", "admin"),
        ).json()
        assert body["code"] == agents_api.AGENT_ILLEGAL_STATE_TRANSITION


# ----------------------------------------------------------------------
# 评测产物（WP-05 契约）
# ----------------------------------------------------------------------

_REAL_EVAL_ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / "artifacts"
    / "agent_evals"
    / "latest_v2.json"
)


class TestEvalLatest:
    def test_eval_missing_file_6008(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            agents_api,
            "_EVAL_JSON_PATH",
            Path("Z:/definitely/not/exist/agent_evals/latest_v2.json"),
        )
        body = client.get(f"{API}/evals/latest").json()
        assert body["code"] == agents_api.AGENT_EVAL_UNAVAILABLE
        assert body["data"] is None

    def test_eval_corrupt_json_6008(
        self,
        client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        eval_file = tmp_path / "latest_v2.json"
        eval_file.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(agents_api, "_EVAL_JSON_PATH", eval_file)
        body = client.get(f"{API}/evals/latest").json()
        assert body["code"] == agents_api.AGENT_EVAL_UNAVAILABLE
        assert body["data"] is None

    def test_eval_missing_required_fields_6008(
        self,
        client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """部分键缺失 → 明确 6008（不得静默用默认值全零展示）。"""
        eval_file = tmp_path / "latest_v2.json"
        eval_file.write_text(
            json.dumps({"schema_version": "1.0", "report_type": "agent_evals"}),
            encoding="utf-8",
        )
        monkeypatch.setattr(agents_api, "_EVAL_JSON_PATH", eval_file)
        body = client.get(f"{API}/evals/latest").json()
        assert body["code"] == agents_api.AGENT_EVAL_UNAVAILABLE
        assert body["data"] is None
        assert "不符合契约" in body["message"]

    def test_eval_sample_size_mismatch_6008(
        self,
        client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """scenario 数量与 sample_size 不一致 → 明确校验失败 6008。"""
        eval_dir = Path(__file__).resolve().parent / "agent_evals"
        if str(eval_dir) not in sys.path:
            sys.path.insert(0, str(eval_dir))
        from contract import sample_report

        payload = sample_report()
        payload["sample_size"] = payload["sample_size"] + 1  # 与 scenarios 数量脱钩
        eval_file = tmp_path / "latest_v2.json"
        eval_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(agents_api, "_EVAL_JSON_PATH", eval_file)
        body = client.get(f"{API}/evals/latest").json()
        assert body["code"] == agents_api.AGENT_EVAL_UNAVAILABLE
        assert "sample_size" in body["message"]

    def test_eval_metrics_out_of_range_6008(
        self,
        client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """指标值域非法（如成功率 1.5）→ 6008，不展示伪造指标。"""
        eval_dir = Path(__file__).resolve().parent / "agent_evals"
        if str(eval_dir) not in sys.path:
            sys.path.insert(0, str(eval_dir))
        from contract import sample_report

        payload = sample_report()
        payload["metrics"]["success_rate"] = 1.5
        eval_file = tmp_path / "latest_v2.json"
        eval_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(agents_api, "_EVAL_JSON_PATH", eval_file)
        body = client.get(f"{API}/evals/latest").json()
        assert body["code"] == agents_api.AGENT_EVAL_UNAVAILABLE

    def test_eval_validates_synthetic_artifact(
        self,
        client: TestClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """合法产物（WP-05 sample_report）→ code 0，全字段正确映射。"""
        eval_dir = Path(__file__).resolve().parent / "agent_evals"
        if str(eval_dir) not in sys.path:
            sys.path.insert(0, str(eval_dir))
        from contract import sample_report

        payload = sample_report()
        eval_file = tmp_path / "latest_v2.json"
        eval_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(agents_api, "_EVAL_JSON_PATH", eval_file)

        body = client.get(f"{API}/evals/latest").json()
        assert body["code"] == 0
        data = body["data"]
        assert data["available"] is True
        assert data["report_type"] == "agent_evals"
        assert data["schema_version"] == "2.0"
        assert data["evidence_level"] == "E1"
        assert data["sample_size"] == 1
        assert data["skipped_count"] == 1
        assert len(data["scenarios"]) == 2
        assert data["code_version"]["value"]
        assert data["config"]["hash"]
        assert data["environment"] == {
            "network_access": False,
            "model_access": False,
            "device_access": False,
            "runtime_backend": "in-memory",
            "persistent_backend": "sqlite-memory",
        }
        assert data["metrics"]["denominators"]["scenarios"] == 1
        scenario = data["scenarios"][0]
        assert scenario["id"] == "sample_scenario"
        assert scenario["passed"] is True
        assert scenario["skipped"] is False
        assert scenario["expected"]["status"] == "succeeded"
        assert scenario["actual"]["status"] == "succeeded"
        assert data["scenarios"][1]["skipped"] is True
        assert data["scenarios"][1]["skip_reason"]
        assert data["source"] == "artifacts/agent_evals/latest_v2.json"

    def test_eval_reads_real_artifact_all_fields(self, client: TestClient) -> None:
        """当前有效 artifact：正确解析全部字段（消费真实契约，禁止占位）。"""
        if not _REAL_EVAL_ARTIFACT.is_file():
            pytest.skip(f"WP-05 尚未产出 {_REAL_EVAL_ARTIFACT}")

        body = client.get(f"{API}/evals/latest").json()
        assert body["code"] == 0
        data = body["data"]
        assert data["available"] is True
        assert data["report_type"] == "agent_evals"
        assert data["schema_version"] == "2.0"
        assert data["evidence_level"] == "E1"
        # 来源元数据
        assert data["command"]
        assert data["date"]
        assert data["code_version"]["value"]
        assert len(data["code_version"]["fingerprint"]) == 64
        assert data["config"]["hash"]
        assert data["config"]["scenario_count"] == data["sample_size"]
        # 离线声明
        assert data["environment"]["network_access"] is False
        assert data["environment"]["model_access"] is False
        assert data["environment"]["device_access"] is False
        # 场景与样本量一致
        assert data["sample_size"] == len(data["scenarios"]) > 0
        # 十三项冻结指标全部真实解析（非占位全零）
        metrics = data["metrics"]
        for key in (
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
        ):
            assert key in metrics
        assert metrics["definitions"]
        assert metrics["denominators"]
        # 指标与分母台账一致（值来自真实统计，不是默认 0）
        assert metrics["denominators"]["scenarios"] == data["sample_size"]
        if metrics["denominators"]["scenarios"]:
            assert abs(
                metrics["invalid_loop_rate"]
                - metrics["denominators"]["invalid_loop_runs"]
                / metrics["denominators"]["scenarios"]
            ) < 1e-9
        # 每场景携带 id/name/expected/actual
        for scenario in data["scenarios"]:
            assert scenario["id"]
            assert scenario["name"]
            assert scenario["passed"] in (True, False)
            assert scenario["expected"]["status"]
            assert scenario["actual"]["status"]
        assert data["source"] == "artifacts/agent_evals/latest_v2.json"


class TestEvalContractAlignment:
    """防漂移：agents.py 内嵌评测契约常量必须与 WP-05 contract.py 冻结清单一致。"""

    def test_required_lists_align_with_wp05_contract(self) -> None:
        eval_dir = Path(__file__).resolve().parent / "agent_evals"
        if str(eval_dir) not in sys.path:
            sys.path.insert(0, str(eval_dir))
        import contract

        assert agents_api.EVAL_SCHEMA_VERSION == contract.REPORT_SCHEMA_VERSION
        assert agents_api.EVAL_REPORT_TYPE == contract.REPORT_TYPE
        assert agents_api.EVAL_EVIDENCE_LEVEL == contract.EVAL_EVIDENCE_LEVEL
        assert agents_api.EVAL_REQUIRED_TOP_LEVEL == contract.REQUIRED_TOP_LEVEL
        assert agents_api.EVAL_REQUIRED_CODE_VERSION == contract.REQUIRED_CODE_VERSION
        assert agents_api.EVAL_REQUIRED_CONFIG == contract.REQUIRED_CONFIG
        assert agents_api.EVAL_REQUIRED_SCENARIO == contract.REQUIRED_SCENARIO
        assert agents_api.EVAL_REQUIRED_EXPECTED == contract.REQUIRED_EXPECTED
        assert agents_api.EVAL_REQUIRED_ACTUAL == contract.REQUIRED_ACTUAL
        assert agents_api.EVAL_REQUIRED_METRICS == contract.REQUIRED_METRICS
        assert (
            agents_api.EVAL_REQUIRED_METRIC_DEFINITIONS
            == contract.REQUIRED_METRIC_DEFINITIONS
        )
        assert agents_api.EVAL_REQUIRED_DENOMINATORS == contract.REQUIRED_DENOMINATORS


# ----------------------------------------------------------------------
# OpenAPI 契约：response_model 与实现一致、无内部对象泄漏
# ----------------------------------------------------------------------


class TestOpenApiResponseModels:
    """所有 Agent 端点声明 ApiResponse 信封 response_model，
    响应 data 只引用声明的 Agent* 组件，不泄漏内核内部对象。"""

    _KNOWN_DATA_COMPONENTS = {
        "AgentRunOut",
        "AgentRunPageOut",
        "AgentStepOut",
        "AgentRuntimeStatusOut",
        "AgentToolOut",
        "AgentApprovalOut",
        "AgentEvalOut",
    }

    @staticmethod
    def _ref_name(node: dict[str, Any]) -> str | None:
        return node["$ref"].rsplit("/", 1)[1] if "$ref" in node else None

    @staticmethod
    def _resolve_props(openapi: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
        for _ in range(10):
            ref = TestOpenApiResponseModels._ref_name(node)
            if ref is None:
                break
            node = openapi["components"]["schemas"][ref]
        return node.get("properties", {})

    def test_all_agent_endpoints_declare_envelope_response_models(self) -> None:
        openapi = real_app.openapi()
        agent_paths = sorted(
            p for p in openapi["paths"] if p.startswith("/api/v1/agents")
        )
        assert len(agent_paths) == 9  # 10 端点 → 9 路径（GET+POST 共用 /runs）
        for path in agent_paths:
            for method, op in openapi["paths"][path].items():
                responses = op.get("responses", {})
                assert "200" in responses, f"{method.upper()} {path} 缺少 200 响应"
                content = responses["200"].get("content", {})
                assert "application/json" in content, f"{method.upper()} {path} 无 json content"
                node = content["application/json"]["schema"]
                props = self._resolve_props(openapi, node)
                for field in ("code", "message", "data", "trace_id"):
                    assert field in props, (
                        f"{method.upper()} {path} 响应不是 ApiResponse 信封（缺 {field}）"
                    )

    def test_data_refs_point_to_declared_agent_components(self) -> None:
        openapi = real_app.openapi()
        schemas = openapi["components"]["schemas"]
        for name, schema in schemas.items():
            if not name.startswith("ApiResponse_Agent"):
                continue
            props = schema.get("properties", {})
            if "data" not in props:
                continue
            data_node = props["data"]
            refs: list[str] = []
            ref = self._ref_name(data_node)
            if ref:
                refs.append(ref)
            for option in data_node.get("anyOf", []):
                ref = self._ref_name(option)
                if ref:
                    refs.append(ref)
            assert refs, f"组件 {name} 的 data 未指向任何响应模型"
            for ref in refs:
                assert ref in self._KNOWN_DATA_COMPONENTS, (
                    f"组件 {name} 的 data 引用了未声明的内部组件 {ref}"
                )

    def test_no_internal_kernel_fields_leak(self) -> None:
        openapi = real_app.openapi()
        internal_keys = {
            "request",
            "runtime_state",
            "handler",
            "executor",
            "registry",
            "policy_guard",
            "memory_store",
            "clock",
            "config",
            "approvals",
        }
        for name, schema in openapi["components"]["schemas"].items():
            props = schema.get("properties", {})
            # 找到 AgentRunOut 形态的组件（run_id/objective/trigger_type）
            if {"run_id", "objective", "trigger_type"} <= set(props):
                overlap = internal_keys & set(props)
                assert not overlap, f"组件 {name} 泄漏内核内部字段：{sorted(overlap)}"


# ----------------------------------------------------------------------
# GET /ai/agents 接入真实 Runtime 状态（手册 3.8 兼容保留）
# ----------------------------------------------------------------------


class TestAiAgentsRealStatus:
    def test_ai_agents_idle_when_no_runtime_data(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.api.v1 import ai

        test_app = FastAPI()
        register_exception_handlers(test_app)
        test_app.include_router(ai.router, prefix="/api/v1/ai")
        agents_api.reset_agent_runtime_for_tests()
        with TestClient(test_app) as test_client:
            payload = test_client.get("/api/v1/ai/agents").json()["data"]
            assert payload
            # 无运行数据 → 全部 idle，绝不写死 running
            assert {item["status"] for item in payload} == {"idle"}

    def test_ai_agents_unavailable_on_runtime_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.api.v1 import ai

        test_app = FastAPI()
        register_exception_handlers(test_app)
        test_app.include_router(ai.router, prefix="/api/v1/ai")

        monkeypatch.setattr(
            agents_api,
            "get_agent_runtime",
            lambda: (_ for _ in ()).throw(RuntimeError("runtime unavailable")),
        )
        with TestClient(test_app) as test_client:
            payload = test_client.get("/api/v1/ai/agents").json()["data"]
            assert {item["status"] for item in payload} == {"unavailable"}

    def test_ai_agents_reflect_real_running_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.api.v1 import ai

        runtime = _make_runtime(require_approval_risk=(RiskLevel.WRITE,))
        runtime.run(
            AgentRunRequest(
                trigger_type="event",
                objective="处置事件 evt_x",
                actor="operator",
                role="operator",
                params={"event_id": "evt_x"},
            )
        )
        _inject_runtime(monkeypatch, runtime)

        test_app = FastAPI()
        register_exception_handlers(test_app)
        test_app.include_router(ai.router, prefix="/api/v1/ai")
        with TestClient(test_app) as test_client:
            payload = test_client.get("/api/v1/ai/agents").json()["data"]
            assert {item["status"] for item in payload} == {"running"}


# ----------------------------------------------------------------------
# 多角色协同（team 计划）
# ----------------------------------------------------------------------
# 这一组钉死三件事：
#   1) 默认（不传 mode）仍是单角色，roles 为空、plan_key 为 single —— 基线不变；
#   2) team 模式下研判 Agent 与调度执行 Agent 都出现在轨迹里，且角色可回放；
#   3) 研判是真门禁：证据不足时 run 安全终止、事件不派单、工单不落库。


def _make_plan_runtime(
    *,
    require_approval_risk: tuple[RiskLevel, ...] = (),
    clock: FakeClock | None = None,
    id_factory: Any = None,
    lessons_enabled: bool = False,
) -> AgentRuntime:
    """带计划变体表 / 经验闭环开关的 Runtime。"""
    runtime = AgentRuntime(
        runtime_config=RuntimeConfig(
            policy_version="rules-work-order-v1.0",
            rule_plan=agents_api.WORK_ORDER_PLAN,
            rule_plans=agents_api.RULE_PLANS,
            lessons_enabled=lessons_enabled,
            model_available=False,
            require_approval_risk=require_approval_risk,
        ),
        clock=clock,
        id_factory=id_factory,
    )
    for tool in agents_api._tool_definitions():
        runtime.registry.register(tool)
    return runtime


def _patch_policy_refs(
    monkeypatch: pytest.MonkeyPatch,
    refs: list[dict[str, Any]] | None = None,
) -> None:
    """替换政策预取：测试不连知识库，直接注入确定性依据。"""

    async def _prefetch(_session: Any, snapshot: agents_api.AgentEventSnapshot) -> None:
        snapshot.policy_query = "foam 漂浮垃圾 清理 打捞 处置 政策 马鼻镇"
        snapshot.policy_refs = list(refs if refs is not None else _POLICY_REFS)

    monkeypatch.setattr(agents_api, "_prefetch_policy_refs", _prefetch)


def _team_snapshot(**kwargs: Any) -> agents_api.AgentEventSnapshot:
    """带证据图的事件快照 —— 研判门禁要求「有证据图 + 置信度达标」。"""
    snapshot = _snapshot(**kwargs)
    snapshot.event.evidence_url = "demo://minio/events/evt_wp03.jpg"
    return snapshot


_POLICY_REFS: list[dict[str, Any]] = [    {
        "asset_id": "KA-DEMO-POLICY-001",
        "asset_version_id": "KAV-DEMO-POLICY-001-V1",
        "title": "连江县海漂垃圾巡查处置指南（演示样本）",
        "asset_type": "document",
        "source_uri": "demo://lianjiang/knowledge/marine-litter-response-guide",
        "score": 12.5,
        "citation": "发现泡沫塑料聚集后，责任区域应启动岸线巡查……再生成打捞任务。",
        "hop_no": 0,
        "matched_node_ids": [],
        "matched_relation_ids": [],
    }
]


class TestTeamMode:
    def test_single_mode_is_unchanged_by_default(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """不传 mode 时仍是单角色管道：plan_key=single、roles 为空。"""
        _inject_runtime(monkeypatch, _make_plan_runtime())
        _patch_snapshot(monkeypatch, _snapshot())

        body = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=_headers("operator", "operator"),
        ).json()["data"]
        assert body["status"] == "succeeded"
        assert body["plan_key"] == "single"
        assert body["roles"] == []
        assert all(step["role"] is None for step in body["steps"])

    def test_team_mode_records_both_roles(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        session: RecordingSession,
    ) -> None:
        """team 模式：研判两步在前、调度四步在后，轨迹带角色且可回放。"""
        _inject_runtime(monkeypatch, _make_plan_runtime())
        _patch_snapshot(monkeypatch, _team_snapshot())
        _patch_policy_refs(monkeypatch)

        body = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03", "mode": "team"},
            headers=_headers("operator", "operator"),
        ).json()["data"]
        assert body["status"] == "succeeded", body
        assert body["plan_key"] == "team"
        assert body["roles"] == [agents_api.ROLE_ASSESSOR, agents_api.ROLE_DISPATCHER]

        # 轨迹来源去重后应包含两个角色的工具步骤
        role_by_step = {step["step_no"]: step["role"] for step in body["steps"]}
        assert role_by_step[1] is None  # plan 步骤不属于任何单一角色
        tool_steps = [s for s in body["steps"] if s["step_type"] == "tool_call"]
        tools = [s["tool_name"] for s in tool_steps]
        assert tools[:2] == ["policy.lookup", "analysis.assess"]
        assert tools[2:] == [
            "event.get",
            "device.query_available",
            "dispatch.plan",
            "task.create_or_merge",
        ]
        assert [s["role"] for s in tool_steps[:2]] == [agents_api.ROLE_ASSESSOR] * 2
        assert [s["role"] for s in tool_steps[2:]] == [agents_api.ROLE_DISPATCHER] * 4

        # 观察步骤同样带角色（回放时读的就是这份映射）
        assert all(
            s["role"] in (agents_api.ROLE_ASSESSOR, agents_api.ROLE_DISPATCHER)
            for s in body["steps"]
            if s["step_type"] == "observation"
        )
        # 计划摘要里明说了角色分工 —— 评委一眼能看到"谁交给了谁"
        plan_step = next(s for s in body["steps"] if s["step_type"] == "plan")
        assert agents_api.ROLE_ASSESSOR in plan_step["decision_summary"]
        assert agents_api.ROLE_DISPATCHER in plan_step["decision_summary"]

        # 研判结论真的驱动了业务：真实工单落库
        assert any(isinstance(item, Task) for item in session.added)

        # 详情与轨迹端点的角色映射一致
        detail = client.get(
            f"{API}/runs/{body['run_id']}",
            headers=_headers("viewer", "viewer"),
        ).json()["data"]
        assert detail["roles"] == body["roles"]
        steps = client.get(
            f"{API}/runs/{body['run_id']}/steps",
            headers=_headers("viewer", "viewer"),
        ).json()["data"]
        assert [s["role"] for s in steps] == [s["role"] for s in body["steps"]]

    def test_team_mode_gate_blocks_dispatch_without_evidence(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        session: RecordingSession,
    ) -> None:
        """研判门禁：无证据图 → manual_review → 安全终止，不派单、不建单。"""
        _inject_runtime(monkeypatch, _make_plan_runtime())
        snapshot = _team_snapshot()
        snapshot.event.evidence_url = None
        _patch_snapshot(monkeypatch, snapshot)
        _patch_policy_refs(monkeypatch)

        body = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03", "mode": "team"},
            headers=_headers("operator", "operator"),
        ).json()["data"]
        assert body["status"] == "failed"
        assert body["termination_reason"] == "policy_denied"
        assert body["plan_key"] == "team"
        # 事件保持 new、没有工单落库
        assert not any(isinstance(item, Task) for item in session.added)
        assert snapshot.event.status == EventStatus.NEW
        assert snapshot.prepared_task is None
        # 研判步骤本身是失败的 tool_call（审计不允许静默丢弃）
        failed = [s for s in body["steps"] if s["status"] == "failed"]
        assert any(s["tool_name"] == "analysis.assess" for s in failed)

    def test_unknown_plan_variant_fails_loudly(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """变体表里没有的 plan_key 必须响亮失败，绝不静默回落成单角色。"""
        _inject_runtime(monkeypatch, _make_plan_runtime())
        _patch_snapshot(monkeypatch, _snapshot())

        # 请求体层已用 Literal 收敛取值，这里直接走内核验证守卫本身
        runtime = _make_plan_runtime()
        request = AgentRunRequest(
            trigger_type="event",
            objective="未知计划",
            actor="operator",
            role="operator",
            params={"event_id": "evt_wp03"},
            plan_key="bogus",
        )
        result = runtime.run(request)
        assert result.status == "failed"
        assert result.termination_reason == "internal_error"

    def test_policy_prefetch_degrades_without_knowledge_base(
        self,
        session: RecordingSession,
    ) -> None:
        """知识库不可用（桩 Session 无 scalar）时降级，绝不抛错中断派单。"""
        snapshot = _snapshot()
        # 直接调真实实现：RecordingSession 没有 .scalar → 内部 AttributeError
        asyncio.run(agents_api._prefetch_policy_refs(session, snapshot))
        assert snapshot.policy_degraded is True
        assert snapshot.policy_refs == []
        assert snapshot.policy_note
        assert snapshot.policy_query

    def test_team_mode_writes_decision_trace(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        session: RecordingSession,
    ) -> None:
        """研判结论与政策依据落成知识决策链（DecisionTrace + 证据）。"""
        from app.models.knowledge import DecisionEvidence, DecisionTrace

        _inject_runtime(monkeypatch, _make_plan_runtime())
        _patch_snapshot(monkeypatch, _team_snapshot())
        _patch_policy_refs(monkeypatch)

        body = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03", "mode": "team"},
            headers=_headers("operator", "operator"),
        ).json()["data"]
        assert body["status"] == "succeeded"

        traces = [item for item in session.added if isinstance(item, DecisionTrace)]
        assert len(traces) == 1
        assert traces[0].run_id == body["run_id"]
        assert traces[0].status == "completed"
        evidence = [item for item in session.added if isinstance(item, DecisionEvidence)]
        assert len(evidence) == len(_POLICY_REFS)
        assert evidence[0].citation_text == _POLICY_REFS[0]["citation"]
        assert "agent_decision_trace" in _audit_actions(session)

    def test_single_mode_writes_no_decision_trace(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        session: RecordingSession,
    ) -> None:
        """单角色 run 不产生决策链，不污染知识模块的决策列表。"""
        from app.models.knowledge import DecisionTrace

        _inject_runtime(monkeypatch, _make_plan_runtime())
        _patch_snapshot(monkeypatch, _snapshot())

        body = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=_headers("operator", "operator"),
        ).json()["data"]
        assert body["status"] == "succeeded"
        assert not any(isinstance(item, DecisionTrace) for item in session.added)

    def test_decision_snapshot_exposes_candidates_and_reason(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """决策快照：候选机器人（含坐标）+ 选中者 + 理由，供大屏地图联动。"""
        _inject_runtime(monkeypatch, _make_plan_runtime())
        _patch_snapshot(monkeypatch, _team_snapshot())
        _patch_policy_refs(monkeypatch)

        body = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03", "mode": "team"},
            headers=_headers("operator", "operator"),
        ).json()["data"]
        assert body["status"] == "succeeded"

        decision = body["decision"]
        assert decision is not None
        assert decision["selected_robot_id"] == "rb_01"
        assert decision["reason"]
        assert decision["recommended_action"] in ("dispatch", "merge_first")
        assert decision["basis"]
        # 候选必须带坐标，否则地图上标不出来
        assert len(decision["candidates"]) == 1
        assert decision["candidates"][0]["lng"] == pytest.approx(119.601)

        # 详情端点给出同一份决策快照
        detail = client.get(
            f"{API}/runs/{body['run_id']}",
            headers=_headers("viewer", "viewer"),
        ).json()["data"]
        assert detail["decision"]["selected_robot_id"] == "rb_01"

    def test_decision_snapshot_is_null_when_run_never_dispatched(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """没走到派单步骤的 run 不拼半张快照 —— decision 为 null。"""
        _inject_runtime(monkeypatch, _make_plan_runtime())
        snapshot = _team_snapshot()
        snapshot.event.evidence_url = None
        _patch_snapshot(monkeypatch, snapshot)
        _patch_policy_refs(monkeypatch)

        body = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03", "mode": "team"},
            headers=_headers("operator", "operator"),
        ).json()["data"]
        assert body["status"] == "failed"
        assert body["decision"] is None


# ----------------------------------------------------------------------
# 跨 run 经验（lessons：确定性「可进化」闭环）
# ----------------------------------------------------------------------


class TestLessons:
    """无模型、无向量的经验闭环：
        复盘提炼 → 落记忆 → 下次规划按条件检索命中 → 写进 plan 摘要。
    这一组把它钉死，避免它退化成"看起来在进化"的装饰。
    """

    def test_lessons_are_harvested_and_referenced_next_run(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = _make_plan_runtime(lessons_enabled=True)
        _inject_runtime(monkeypatch, runtime)
        operator = _headers("operator", "operator")
        viewer = _headers("viewer", "viewer")

        # 第一次：近邻已有可合并工单 → 工具链走 merged 分支 → 复盘点出「合并优先」
        _patch_snapshot(
            monkeypatch,
            _team_snapshot(merge_task_id="task_existing"),
        )
        first = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=operator,
        ).json()["data"]
        assert first["status"] == "succeeded"
        assert first["task_action"] == "merged"
        # 首次运行时经验库还是空的
        assert first["lesson_hits"] == []

        status = client.get(f"{API}/runtime/status", headers=viewer).json()["data"]
        kinds = {(item["scope_id"], item["kind"]) for item in status["lessons"]}
        assert ("foam", "merge_preferred") in kinds

        # 第二次：同类事件同样有可合并工单 → 命中经验，写进 plan 摘要
        _patch_snapshot(
            monkeypatch,
            _team_snapshot(merge_task_id="task_existing"),
        )
        second = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=operator,
        ).json()["data"]
        assert second["status"] == "succeeded"
        assert second["lesson_hits"], "第二次运行应当引用到上一步沉淀的经验"
        plan_step = next(s for s in second["steps"] if s["step_type"] == "plan")
        assert "本次引用经验" in plan_step["decision_summary"]

        status = client.get(f"{API}/runtime/status", headers=viewer).json()["data"]
        lesson = next(
            item
            for item in status["lessons"]
            if item["scope_id"] == "foam" and item["kind"] == "merge_preferred"
        )
        assert lesson["hit_count"] == 1
        # 置信度随命中单调上调，但永远封顶在 95% 以下（启发式不是定理）
        assert 0.85 < lesson["confidence"] <= 0.95
        assert lesson["source_run_id"] == second["run_id"]

    def test_lesson_not_referenced_when_condition_does_not_hold(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """条件不满足就不引用 —— 经验不是"见到同类就贴上去"的装饰。"""
        runtime = _make_plan_runtime(lessons_enabled=True)
        _inject_runtime(monkeypatch, runtime)
        operator = _headers("operator", "operator")

        _patch_snapshot(monkeypatch, _team_snapshot(merge_task_id="task_existing"))
        assert (
            client.post(f"{API}/runs", json={"event_id": "evt_wp03"}, headers=operator)
            .json()["data"]["status"]
            == "succeeded"
        )

        # 这次没有可合并工单 → merge_candidate=false → 合并经验不适用
        _patch_snapshot(monkeypatch, _team_snapshot())
        second = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=operator,
        ).json()["data"]
        assert second["status"] == "succeeded"
        assert second["lesson_hits"] == []
        plan_step = next(s for s in second["steps"] if s["step_type"] == "plan")
        assert "本次引用经验" not in plan_step["decision_summary"]

    def test_cancelled_run_harvests_nothing(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """取消是"人的决定"，复盘不出可复用规律 —— 不硬编一条经验出来。"""
        runtime = _make_plan_runtime(
            lessons_enabled=True,
            require_approval_risk=(RiskLevel.WRITE,),
        )
        _inject_runtime(monkeypatch, runtime)
        _patch_snapshot(monkeypatch, _team_snapshot(merge_task_id="task_existing"))
        operator = _headers("operator", "operator")

        started = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=operator,
        ).json()["data"]
        assert started["status"] == "waiting_approval"
        client.post(
            f"{API}/runs/{started['run_id']}/cancel",
            json={"reason": "演示取消"},
            headers=operator,
        )
        assert len(runtime.lessons) == 0

    def test_lessons_disabled_by_default_keeps_eval_baseline_intact(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """不开开关时内核行为逐字节不变：不检索、不沉淀、摘要里没有经验段。"""
        runtime = _make_plan_runtime()  # lessons_enabled=False
        _inject_runtime(monkeypatch, runtime)
        _patch_snapshot(monkeypatch, _team_snapshot(merge_task_id="task_existing"))

        body = client.post(
            f"{API}/runs",
            json={"event_id": "evt_wp03"},
            headers=_headers("operator", "operator"),
        ).json()["data"]
        assert body["status"] == "succeeded"
        assert body["lesson_hits"] == []
        assert len(runtime.lessons) == 0
        plan_step = next(s for s in body["steps"] if s["step_type"] == "plan")
        assert "本次引用经验" not in plan_step["decision_summary"]
        status = client.get(
            f"{API}/runtime/status",
            headers=_headers("viewer", "viewer"),
        ).json()["data"]
        assert status["lessons"] == []

    def test_lesson_store_confidence_is_monotonic_and_capped(self) -> None:
        """纯内核单测：置信度随证据累积单调上调且封顶，绝不出现 1.0。"""
        store = LessonStore(InMemoryMemoryStore(), enabled=True)
        for i in range(12):
            lessons = store.harvest(
                run_id=f"run_{i}",
                main_class="foam",
                outcome="succeeded",
                termination_reason="succeeded",
                bindings={"action": "merged"},
            )
            assert [item.kind for item in lessons] == ["merge_preferred"]
        lesson = store.all_lessons()[0]
        # 首次是"提炼"（0 引用 0 确认），之后 11 次是"再次确认"
        assert lesson.hit_count == 0
        assert lesson.confirm_count == 11
        assert lesson.confidence == 0.95
        # 记忆里留痕：每条经验状态变化都追加一条 episodic 记录（不是就地改写）
        logged = store.memory.find(
            scope_type=LESSON_SCOPE_TYPE,
            scope_id="foam",
            memory_type=LESSON_MEMORY_TYPE,
        )
        assert len(logged) == 12


class TestRuntimeStatusExtras:
    def test_status_exposes_approval_policy_and_plan_variants(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """演示前自检字段：审批策略取自内核实际配置，计划变体来自变体表。"""
        runtime = _make_plan_runtime(
            require_approval_risk=(
                RiskLevel.WRITE,
                RiskLevel.DEVICE_COMMAND,
                RiskLevel.SENSITIVE,
            )
        )
        _inject_runtime(monkeypatch, runtime)
        payload = client.get(
            f"{API}/runtime/status",
            headers=_headers("viewer", "viewer"),
        ).json()["data"]

        assert payload["require_approval_for_write"] is True
        assert set(payload["approval_risk_levels"]) == {
            "write",
            "device_command",
            "sensitive",
        }
        assert payload["plan_variants"] == ["single", "team"]

    def test_status_default_policy_excludes_write(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """默认策略下 WRITE 不需要审批 —— 与生产策略一致。"""
        _inject_runtime(monkeypatch, _make_plan_runtime())
        payload = client.get(
            f"{API}/runtime/status",
            headers=_headers("viewer", "viewer"),
        ).json()["data"]
        assert payload["require_approval_for_write"] is False
        assert "write" not in payload["approval_risk_levels"]

