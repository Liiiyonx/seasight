"""WP-10 持久化仓储验收测试（SQLite 内存，无 PostgreSQL 也可跑）。

覆盖规格第 4 节 8 个必测场景：
    1. 创建 run 后新仓储实例可读取。
    2. 等待审批的 run 重启后可通过 resume() 继续。
    3. 同一幂等键并发触发只创建一个 run。
    4. 旧 state_version 保存冲突（抛 TaskConflictError）。
    5. 重复 (run_id, step_no) 写入失败。
    6. 审批决策跨仓储实例可见且不可重复覆盖。
    7. 数据库异常时事务回滚（不残留半写数据）。
    8. 敏感字段脱敏：持久化内容不得出现 api_key / authorization /
       chain_of_thought / reasoning。

引擎可注入：本文件使用 SQLite 内存 + StaticPool（单连接共享），
保证「跨仓储实例」读同一数据库；无需任何外部 PostgreSQL。

★ 不访问公网、不操作任何真实数据库；仅本测试进程内的 SQLite 内存。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.sql.sqltypes import BigInteger

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(type_, compiler, **kw):  # noqa: ANN001
    """SQLite 适配：BIGINT 编译为 INTEGER，使主键列成为 rowid 别名并自动递增。

    SQLite 只对类型恰为 INTEGER 的主键列做 rowid 别名自动递增；BIGINT 主键
    插入时不带 id 会报 NOT NULL constraint failed。t_agent_* 表主键均为
    BigInteger（PostgreSQL 上为 BIGSERIAL 语义），本编译覆写仅在 SQLite
    方言生效，使「无 PostgreSQL 也能跑」的内存测试成立；PostgreSQL 不受影响。
    """
    return "INTEGER"

from app.db.session import Base  # noqa: E402
from app.models import agent as agent_models  # noqa: E402,F401  注册 t_agent_run/step/approval
from app.models import agent_state  # noqa: E402,F401             注册 t_agent_run_state
from app.services.agents.errors import TaskConflictError  # noqa: E402
from app.services.agents.model import (  # noqa: E402
    AgentRun,
    AgentRunRequest,
    AgentStep,
    RiskLevel,
    RuntimeConfig,
)
from app.services.agents.persistent_repository import (  # noqa: E402
    SqlAlchemyApprovalRepository,
    SqlAlchemyRunRepository,
)
from app.services.agents.repositories import ApprovalRecord  # noqa: E402
from app.services.agents.runtime import AgentRuntime, FakeClock, UuidIdFactory  # noqa: E402
from app.services.agents.tools import ToolDefinition, ToolRegistry  # noqa: E402

# ----------------------------------------------------------------------
# 共享 fixture：SQLite 内存引擎（StaticPool 单连接，跨实例共享同一库）
# ----------------------------------------------------------------------


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ANN001,ANN202
        """SQLite 默认不强制外键；打开以贴近 PostgreSQL 的严格语义。"""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # 只建本测试需要的 Agent 表。全库模型在其他测试导入后可能包含
    # PostgreSQL JSONB/Geometry 等 SQLite 无法渲染的类型，不能依赖全局
    # metadata 创建全部业务表。
    Base.metadata.create_all(
        eng,
        tables=[
            agent_models.AgentRun.__table__,
            agent_models.AgentStep.__table__,
            agent_models.AgentMemory.__table__,
            agent_models.AgentApproval.__table__,
            agent_state.AgentRunState.__table__,
        ],
    )
    yield eng
    eng.dispose()


@pytest.fixture()
def factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


# ----------------------------------------------------------------------
# 确定性构造工具
# ----------------------------------------------------------------------


def make_run(run_id: str, *, idem_key: str | None = None, params: dict | None = None,
             objective: str = "处理海漂垃圾事件并完成派单闭环") -> AgentRun:
    now = FakeClock().now()
    return AgentRun(
        run_id=run_id,
        trigger_type="event",
        objective=objective,
        status="created",
        request=AgentRunRequest(
            trigger_type="event",
            objective=objective,
            actor="operator-01",
            role="operator",
            params=params if params is not None else {"event_id": "evt_001"},
            idempotency_key=idem_key,
            trace_id="trace-001",
        ),
        policy_version="rules-v1.0",
        started_at=now,
        finished_at=None,
        termination_reason=None,
        trace_id="trace-001",
        created_at=now,
        updated_at=now,
        runtime_state={"stage": "created"},
    )


def make_step(run_id: str, step_no: int, step_id: str) -> AgentStep:
    return AgentStep(
        step_id=step_id,
        run_id=run_id,
        step_no=step_no,
        step_type="plan",
        decision_summary="规则计划 1 步：event.get",
        created_at=FakeClock().now(),
    )


def make_mini_registry():
    """最小派单注册表：event.get（只读）→ 正常成功。"""
    registry = ToolRegistry()
    calls: dict[str, int] = {}

    def handler(ctx):
        calls["event.get"] = calls.get("event.get", 0) + 1
        return {"event_id": ctx.input["event_id"], "confirmed": True}

    registry.register(
        ToolDefinition(
            name="event.get",
            version="1.0.0",
            description="读取事件与证据",
            input_schema={
                "type": "object",
                "required": ["event_id"],
                "properties": {"event_id": {"type": "string"}},
            },
            output_schema={
                "type": "object",
                "required": ["event_id"],
                "properties": {"event_id": {"type": "string"}},
            },
            risk_level=RiskLevel.READ_ONLY,
            timeout_ms=5000,
            idempotent=False,
            allowed_roles=("operator", "dispatcher"),
            handler=handler,
        )
    )
    return registry, calls


def make_approval_registry():
    """含敏感设备指令的计划（mqtt.send_task 需要审批）。"""
    registry, calls = make_mini_registry()
    calls["mqtt.send_task"] = 0

    def send_handler(ctx):
        calls["mqtt.send_task"] += 1
        return {"sent": True}

    registry.register(
        ToolDefinition(
            name="mqtt.send_task",
            version="1.0.0",
            description="下发设备指令",
            input_schema={
                "type": "object",
                "required": ["task_id"],
                "properties": {"task_id": {"type": "string"}},
            },
            output_schema={
                "type": "object",
                "required": ["sent"],
                "properties": {"sent": {"type": "boolean"}},
            },
            risk_level=RiskLevel.SENSITIVE,
            timeout_ms=5000,
            idempotent=False,
            allowed_roles=("operator", "dispatcher"),
            handler=send_handler,
        )
    )
    return registry, calls


def approval_config() -> RuntimeConfig:
    return RuntimeConfig(
        rule_plan=[
            {"tool": "event.get", "input": {"event_id": "{event_id}"}, "summary": "读取事件"},
            {
                "tool": "mqtt.send_task",
                "input": {"task_id": "{task_id}"},
                "summary": "下发设备指令",
            },
        ],
        require_approval_risk=(RiskLevel.SENSITIVE,),
    )


# ======================================================================
# 1. 创建 run 后新仓储实例可读取
# ======================================================================


class TestNewInstanceReads:
    def test_created_run_readable_by_fresh_instance(self, engine, factory):
        repo1 = SqlAlchemyRunRepository(engine, factory)
        run = make_run("run_0001", idem_key="idem-0001")
        run.runtime_state = {
            "stage": "waiting_approval",
            "plan_idx": 2,
            "replan_count": 0,
            "task_result": {"task_id": "task_0001", "action": "created"},
        }
        repo1.create(run)
        repo1.add_step(make_step("run_0001", 1, "stp_0001"))

        # 全新仓储实例（同一引擎/工厂）从数据库读取
        repo2 = SqlAlchemyRunRepository(engine, factory)
        loaded = repo2.get("run_0001")
        assert loaded is not None
        assert loaded.run_id == "run_0001"
        assert loaded.status == "created"
        assert loaded.objective == run.objective
        assert loaded.request.actor == "operator-01"
        assert loaded.request.params == {"event_id": "evt_001"}
        assert loaded.runtime_state["stage"] == "waiting_approval"
        assert loaded.runtime_state["plan_idx"] == 2
        assert loaded.runtime_state["replan_count"] == 0
        assert loaded.runtime_state["task_result"] == {
            "task_id": "task_0001",
            "action": "created",
        }

        steps = repo2.steps("run_0001")
        assert len(steps) == 1
        assert steps[0].step_no == 1
        assert steps[0].step_id == "stp_0001"

        assert repo2.find_by_idempotency_key("idem-0001").run_id == "run_0001"
        assert repo2.count() == 1
        assert repo2.list(status="created")[0].run_id == "run_0001"
        repo1.close()
        repo2.close()

    def test_protocol_methods_present(self):
        """实现现有 RunRepository / ApprovalRepository 协议的全部方法。"""
        run_required = {
            "create", "save", "get", "list",
            "find_by_idempotency_key", "add_step", "steps", "count",
        }
        approval_required = {"create", "get", "list_pending", "list_all", "decide", "count"}
        for method in run_required:
            assert callable(getattr(SqlAlchemyRunRepository, method, None)), method
        for method in approval_required:
            assert callable(getattr(SqlAlchemyApprovalRepository, method, None)), method


# ======================================================================
# 2. 等待审批的 run 重启后可通过 resume() 继续
# ======================================================================


class TestRestartResume:
    def test_waiting_approval_run_resumes_after_restart(self, engine, factory):
        registry, calls = make_approval_registry()
        config = approval_config()

        # —— 第一进程：创建 run 并停到 waiting_approval ——
        run_repo1 = SqlAlchemyRunRepository(engine, factory)
        approval_repo1 = SqlAlchemyApprovalRepository(engine, factory)
        runtime1 = AgentRuntime(
            tool_registry=registry,
            run_repository=run_repo1,
            approval_repository=approval_repo1,
            clock=FakeClock(),
            id_factory=UuidIdFactory(),
            runtime_config=config,
        )
        request = AgentRunRequest(
            trigger_type="event",
            objective="处理海漂垃圾事件并完成派单闭环",
            actor="operator-01",
            role="operator",
            params={"event_id": "evt_001", "task_id": "tsk_0001"},
            idempotency_key="idem-restart",
        )
        result = runtime1.run(request)
        assert result.status == "waiting_approval"
        assert len(result.pending_approval_ids) == 1
        approval_id = result.pending_approval_ids[0]
        run_id = result.run_id
        assert approval_repo1.count() == 1
        assert len(run_repo1.steps(run_id)) == 3  # plan / policy / approval_request

        # —— “重启”：全新仓储实例 + 全新 runtime ——
        run_repo2 = SqlAlchemyRunRepository(engine, factory)
        approval_repo2 = SqlAlchemyApprovalRepository(engine, factory)
        runtime2 = AgentRuntime(
            tool_registry=registry,
            run_repository=run_repo2,
            approval_repository=approval_repo2,
            clock=FakeClock(),
            id_factory=UuidIdFactory(),
            runtime_config=config,
        )
        result2 = runtime2.resume(run_id)
        assert result2.status == "waiting_approval"
        assert result2.pending_approval_ids == [approval_id], "重启后必须复用同一条审批，不得重建"

        # 审批决策（新进程/新实例）→ resume 续跑成功
        approval_repo2.decide(approval_id, "admin", "approved", "确认派发")
        result3 = runtime2.resume(run_id)
        assert result3.status == "succeeded"
        assert result3.error_code is None
        assert calls["event.get"] == 1
        assert calls["mqtt.send_task"] == 1

        # 轨迹无重复：plan / policy / approval_request 各恰好一条；审批仍只有一条
        types = [s.step_type for s in result3.steps]
        assert types.count("plan") == 1
        assert types.count("policy") == 1
        assert types.count("approval_request") == 1
        assert types[-1] == "terminal"
        assert approval_repo2.count() == 1
        # 终态已持久化，第三次 resume 是幂等 no-op
        assert runtime2.resume(run_id).status == "succeeded"
        run_repo1.close()
        run_repo2.close()
        approval_repo1.close()
        approval_repo2.close()


# ======================================================================
# 3. 同一幂等键并发触发只创建一个 run
# ======================================================================


class TestIdempotentCreate:
    def test_concurrent_same_key_single_run(self, engine, factory):
        repo1 = SqlAlchemyRunRepository(engine, factory)
        repo2 = SqlAlchemyRunRepository(engine, factory)
        repo1.create(make_run("run_0001", idem_key="idem-race"))
        # 第二个“进程”用不同 run_id 但同一幂等键并发创建 → 必须失败且不留残行
        with pytest.raises(TaskConflictError) as exc_info:
            repo2.create(make_run("run_0002", idem_key="idem-race"))
        assert "幂等键重复" in str(exc_info.value)
        assert repo1.count() == 1
        fresh = SqlAlchemyRunRepository(engine, factory)
        assert fresh.find_by_idempotency_key("idem-race").run_id == "run_0001"
        assert fresh.get("run_0002") is None
        repo1.close()
        repo2.close()
        fresh.close()

    def test_runtime_repeated_trigger_reuses_run(self, engine, factory):
        registry, _ = make_mini_registry()
        config = RuntimeConfig(
            rule_plan=[
                {
                    "tool": "event.get",
                    "input": {"event_id": "{event_id}"},
                    "summary": "读取事件",
                    "expect": {"key": "event_id", "error_code": "tool_failed"},
                }
            ]
        )
        run_repo = SqlAlchemyRunRepository(engine, factory)
        approval_repo = SqlAlchemyApprovalRepository(engine, factory)
        runtime = AgentRuntime(
            tool_registry=registry,
            run_repository=run_repo,
            approval_repository=approval_repo,
            clock=FakeClock(),
            id_factory=UuidIdFactory(),
            runtime_config=config,
        )
        request = AgentRunRequest(
            trigger_type="event",
            objective="处理海漂垃圾事件",
            actor="operator-01",
            role="operator",
            params={"event_id": "evt_001"},
            idempotency_key="idem-trigger",
        )
        r1 = runtime.run(request)
        assert r1.status == "succeeded"
        r2 = runtime.run(request)  # 同键重复触发
        assert r2.run_id == r1.run_id
        assert r2.idempotent_replay is True
        assert run_repo.count() == 1
        run_repo.close()
        approval_repo.close()


# ======================================================================
# 4. 旧 state_version 保存冲突（抛 TaskConflictError）
# ======================================================================


class TestStaleVersion:
    def test_stale_state_version_save_conflict(self, engine, factory):
        repo_a = SqlAlchemyRunRepository(engine, factory)
        run = make_run("run_0001")
        repo_a.create(run)  # v1

        repo_b = SqlAlchemyRunRepository(engine, factory)
        loaded_b = repo_b.get("run_0001")  # 观察 v1
        assert loaded_b is not None

        run.status = "executing"
        repo_a.save(run)  # v1 → v2

        # repo_b 仍持有 v1 的观察 → 保存必须冲突且不覆盖
        loaded_b.status = "succeeded"
        with pytest.raises(TaskConflictError) as exc_info:
            repo_b.save(loaded_b)
        assert "状态版本冲突" in str(exc_info.value)

        # 原数据未被静默覆盖：状态仍为 executing、版本 v2
        fresh = SqlAlchemyRunRepository(engine, factory).get("run_0001")
        assert fresh.status == "executing"
        with engine.connect() as conn:
            version = conn.execute(
                text("select state_version from t_agent_run_state where run_id='run_0001'")
            ).scalar_one()
        assert version == 2
        repo_a.close()
        repo_b.close()


# ======================================================================
# 5. 重复 (run_id, step_no) 写入失败
# ======================================================================


class TestStepUniqueness:
    def test_duplicate_run_step_no_rejected(self, engine, factory):
        repo = SqlAlchemyRunRepository(engine, factory)
        repo.create(make_run("run_0001"))
        repo.add_step(make_step("run_0001", 1, "stp_0001"))
        # 同一 (run_id, step_no) 不同 step_id → 必须抛冲突，历史步骤保留
        with pytest.raises(TaskConflictError):
            repo.add_step(make_step("run_0001", 1, "stp_0002"))
        steps = repo.steps("run_0001")
        assert len(steps) == 1
        assert steps[0].step_id == "stp_0001"
        repo.close()


# ======================================================================
# 6. 审批决策跨仓储实例可见且不可重复覆盖
# ======================================================================


class TestApprovalPersistence:
    def test_decision_cross_instance_no_overwrite(self, engine, factory):
        run_repo = SqlAlchemyRunRepository(engine, factory)
        run_repo.create(make_run("run_0001"))
        ap1 = SqlAlchemyApprovalRepository(engine, factory)
        ap2 = SqlAlchemyApprovalRepository(engine, factory)
        record = ApprovalRecord(
            approval_id="apr_0001",
            run_id="run_0001",
            requested_action="mqtt.send_task：下发设备指令",
            risk_level="device_command",
            requested_by="operator-01",
            requested_at=FakeClock().now(),
        )
        ap1.create(record)

        got = ap2.get("apr_0001")  # 跨实例可见
        assert got is not None
        assert got.decision is None

        decided = ap2.decide("apr_0001", "admin-a", "approved", "确认")
        assert decided.decision == "approved"
        assert ap1.get("apr_0001").decided_by == "admin-a"  # 决策跨实例可见

        with pytest.raises(TaskConflictError):
            ap1.decide("apr_0001", "admin-b", "rejected", "尝试覆盖")  # 不可重复覆盖

        final = ap2.get("apr_0001")
        assert final.decision == "approved"
        assert final.decided_by == "admin-a"
        assert ap2.list_pending("run_0001") == []
        assert len(ap2.list_all()) == 1
        assert ap2.count() == 1
        run_repo.close()
        ap1.close()
        ap2.close()


# ======================================================================
# 7. 数据库异常时事务回滚
# ======================================================================


class TestTransactionRollback:
    def test_create_conflict_rolls_back_atomically(self, engine, factory):
        repo = SqlAlchemyRunRepository(engine, factory)
        repo.create(make_run("run_0001", idem_key="idem-shared"))
        # run 行与 state 行在同一事务：state 唯一索引冲突 → 整个事务回滚
        with pytest.raises(TaskConflictError):
            repo.create(make_run("run_0002", idem_key="idem-shared"))
        assert repo.get("run_0002") is None
        assert repo.count() == 1
        # 底层确认：run_b 的 t_agent_run 与 t_agent_run_state 都没有残行
        with engine.connect() as conn:
            n_run = conn.execute(
                text("select count(*) from t_agent_run where run_id='run_0002'")
            ).scalar_one()
            n_state = conn.execute(
                text("select count(*) from t_agent_run_state where run_id='run_0002'")
            ).scalar_one()
        assert n_run == 0 and n_state == 0
        # 原数据完好
        assert repo.get("run_0001").status == "created"
        repo.close()

    def test_step_conflict_rolls_back_and_keeps_history(self, engine, factory):
        repo = SqlAlchemyRunRepository(engine, factory)
        repo.create(make_run("run_0001"))
        repo.add_step(make_step("run_0001", 1, "stp_0001"))
        with pytest.raises(TaskConflictError):
            repo.add_step(make_step("run_0001", 1, "stp_0002"))
        with engine.connect() as conn:
            n = conn.execute(
                text("select count(*) from t_agent_step where run_id='run_0001'")
            ).scalar_one()
        assert n == 1  # 冲突回滚后不残留半写步骤
        repo.close()


# ======================================================================
# 8. 敏感字段脱敏
# ======================================================================


class TestSensitiveRedaction:
    def test_persisted_content_has_no_sensitive_terms(self, engine, factory):
        params = {
            "event_id": "evt_001",
            "api_key": "sk-live-1234567890",
            "authorization": "Bearer abc123",
            "chain_of_thought": "模型私有推理……",
            "reasoning": "内部推理……",
        }
        run = make_run(
            "run_0001",
            params=params,
            objective="处理事件，authorization 校验通过后派单",
        )
        run.runtime_state = {
            "stage": "executing",
            "plan_idx": 1,
            "replan_count": 0,
            "approvals": ["apr_0001"],
            "bindings": {
                "event_id": "evt_001",
                "api_key": "sk-live",
                "authorization": "Bearer x",
                "chain_of_thought": "cot",
                "reasoning": "why",
            },
        }
        repo = SqlAlchemyRunRepository(engine, factory)
        repo.create(run)

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "select request_json, runtime_state_json "
                    "from t_agent_run_state where run_id='run_0001'"
                )
            ).one()
        blob = (row[0] + "\n" + row[1]).lower()
        for term in ("api_key", "authorization", "chain_of_thought", "reasoning"):
            assert term not in blob, f"持久化内容泄漏敏感字段：{term}"

        # 合法业务摘要仍在
        assert "evt_001" in row[1]
        assert "apr_0001" in row[1]

        # 读回的对象也不含敏感键
        loaded = repo.get("run_0001")
        assert "api_key" not in loaded.runtime_state.get("bindings", {})
        assert "authorization" not in loaded.request.params
        repo.close()
