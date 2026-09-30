"""WP-12 第三波场景执行测试：确定性可复现，接口缺失时明确跳过。

覆盖（docs/agent-program-wave3.md 3.3）：
- 10 个冻结第三波场景逐一执行：passed 或 skipped（skip_reason 非空），
  不允许「静默失败」或「伪造通过」。
- 每个场景的关键语义断言（重启续跑 / 幂等冲突 / 模型回退 /
  敏感工具不执行 / 回放匹配 / 角色交接 / 设备故障恢复率）。
- 真实并发（线程 + 屏障）下同一幂等键最多一个 run（SQLite 临时文件）。
- 接口缺失时场景明确跳过（monkeypatch _try_import 强制模拟）。

全部离线：SQLite 内存/临时文件、假时钟、假模型客户端、内存 device
transport；无公网、无真实模型、无真实设备。证据等级 E1。
"""

from __future__ import annotations

import sys
import threading
import tempfile
from pathlib import Path

import pytest

_EVAL_DIR = Path(__file__).resolve().parent
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

import scenarios as scenarios_mod  # noqa: E402
from scenarios import REQUIRED_WAVE3_SCENARIO_IDS  # noqa: E402


class TestWave3ScenariosExecute:
    """每个冻结第三波场景必须执行通过，或依赖缺失时明确跳过。"""

    @pytest.mark.parametrize("scenario_id", REQUIRED_WAVE3_SCENARIO_IDS)
    def test_wave3_scenario_passes_or_skips(self, scenario_id):
        result = scenarios_mod.run_scenario(scenario_id)
        assert isinstance(result.skipped, bool)
        if result.skipped:
            assert result.passed is False, "跳过场景不得标记为通过"
            assert result.skip_reason and isinstance(result.skip_reason, str), (
                "跳过场景必须有 skip_reason"
            )
        else:
            assert result.passed, (
                f"{scenario_id} 未通过：{result.notes}"
            )
            assert result.actual_status is not None

    def test_v1_scenario_ids_unchanged(self):
        """原 13 个场景的 ID 必须原样保留（只追加，不替换）。"""
        ids = [sc["id"] for sc in scenarios_mod.SCENARIOS]
        v1 = [
            "normal_dispatch_success",
            "no_robot_available",
            "policy_denied",
            "approval_rejected",
            "approval_timeout",
            "approval_approved",
            "tool_timeout",
            "max_steps_exceeded",
            "invalid_loop_replan",
            "replan_recovery_success",
            "rule_mode_without_model",
            "idempotent_replay",
            "invalid_tool_output",
        ]
        for vid in v1:
            assert vid in ids, f"既有场景被替换/删除：{vid}"


class TestPersistentScenarios:
    """WP-10 持久化：重启续跑 / 并发幂等 / 旧版本乐观锁。"""

    def test_restart_resume_succeeds(self):
        result = scenarios_mod.run_scenario("persistent_restart_resume")
        if result.skipped:
            pytest.skip(f"WP-10 接口缺失：{result.skip_reason}")
        assert result.actual_status == "succeeded"
        persist = result.extra["persist"]
        assert persist == {"restart_attempted": 1, "restart_succeeded": 1}
        types = result.step_types
        assert "approval_request" in types
        assert types[0] == "plan" and types[-1] == "terminal"

    def test_concurrent_idempotent_single_run(self):
        result = scenarios_mod.run_scenario("concurrent_idempotent_trigger")
        if result.skipped:
            pytest.skip(f"WP-10 接口缺失：{result.skip_reason}")
        assert result.extra["idem_conflict"] == {"attempts": 1, "conflicts": 1}
        assert len(set(result.extra["run_ids"])) == 1  # 重复触发复用同一 run

    def test_stale_state_version_conflict(self):
        result = scenarios_mod.run_scenario("stale_state_conflict")
        if result.skipped:
            pytest.skip(f"WP-10 接口缺失：{result.skip_reason}")
        assert result.extra["idem_conflict"]["conflicts"] == 1

    def test_threaded_same_idempotency_key_single_run(self):
        """真实并发：两个线程同时创建同一幂等键 → 最多一个 run。"""
        try:
            from app.services.agents.persistent_repository import SqlAlchemyRunRepository
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"WP-10 接口缺失：{exc}")
        from sqlalchemy import create_engine

        from app.models import agent as agent_models
        from app.models import agent_state as agent_state_models
        from app.services.agents import AgentRun, AgentRunRequest, FakeClock, TaskConflictError

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "concurrent.db"
            engine = create_engine(
                f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
            )
            # 注册 SQLite BigInteger 编译（与 scenarios._make_sqlite_engine 同款）
            from sqlalchemy.ext.compiler import compiles
            from sqlalchemy.sql.sqltypes import BigInteger

            @compiles(BigInteger, "sqlite")
            def _bigint_sqlite(type_, compiler, **kw):  # noqa: ANN001, ANN202
                return "INTEGER"

            from app.db.session import Base

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
            from sqlalchemy.orm import sessionmaker

            factory = sessionmaker(bind=engine, expire_on_commit=False)
            repo1 = SqlAlchemyRunRepository(engine, factory)
            repo2 = SqlAlchemyRunRepository(engine, factory)
            barrier = threading.Barrier(2)
            outcomes: list[str] = []

            def make_run(run_id: str) -> AgentRun:
                now = FakeClock().now()
                return AgentRun(
                    run_id=run_id,
                    trigger_type="event",
                    objective="并发幂等测试",
                    status="created",
                    request=AgentRunRequest(
                        trigger_type="event",
                        objective="并发幂等测试",
                        actor="operator-01",
                        role="operator",
                        params={"event_id": "evt_001"},
                        idempotency_key="idem-thread-race",
                    ),
                    policy_version="rules-v1.0",
                    started_at=now,
                    finished_at=None,
                    termination_reason=None,
                    trace_id="trace-thread",
                    created_at=now,
                    updated_at=now,
                    runtime_state={"stage": "created"},
                )

            def worker(repo: SqlAlchemyRunRepository, run_id: str) -> None:
                try:
                    barrier.wait(timeout=10)
                    repo.create(make_run(run_id))
                    outcomes.append("created")
                except TaskConflictError:
                    outcomes.append("conflict")
                except Exception as exc:  # noqa: BLE001 —— 非预期异常如实上报
                    outcomes.append(f"unexpected:{type(exc).__name__}")

            t1 = threading.Thread(target=worker, args=(repo1, "run_t1"))
            t2 = threading.Thread(target=worker, args=(repo2, "run_t2"))
            t1.start()
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)

            fresh = SqlAlchemyRunRepository(engine, factory)
            assert fresh.count() == 1, f"同一幂等键应只创建 1 个 run，实际 {fresh.count()}"
            assert outcomes.count("created") == 1, f"应恰好 1 个创建成功：{outcomes}"
            assert outcomes.count("conflict") == 1, f"应恰好 1 个冲突：{outcomes}"
            assert not [o for o in outcomes if o.startswith("unexpected:")], f"出现非预期异常：{outcomes}"
            repo1.close()
            repo2.close()
            fresh.close()
            engine.dispose()


class TestModelScenarios:
    """WP-11 模型适配：合法计划 / 非法 JSON / 超时 / 敏感工具越权。"""

    def test_valid_plan_source_model(self):
        result = scenarios_mod.run_scenario("model_valid_plan")
        if result.skipped:
            pytest.skip(f"WP-11 接口缺失：{result.skip_reason}")
        prop = result.extra["model_proposal"]
        assert prop["source"] == "model"
        assert prop["error_code"] is None
        assert len(prop["step_tools"]) == 5
        assert result.actual_status == "succeeded"
        assert result.extra["model"]["fallbacks"] == 0

    def test_invalid_json_fallback(self):
        result = scenarios_mod.run_scenario("model_invalid_json_fallback")
        if result.skipped:
            pytest.skip(f"WP-11 接口缺失：{result.skip_reason}")
        prop = result.extra["model_proposal"]
        assert prop["source"] == "rule_fallback"
        assert prop["error_code"] == "model_invalid_json"
        assert prop["fallback_reason"]
        assert result.actual_status == "succeeded"

    def test_timeout_fallback(self):
        result = scenarios_mod.run_scenario("model_timeout_fallback")
        if result.skipped:
            pytest.skip(f"WP-11 接口缺失：{result.skip_reason}")
        prop = result.extra["model_proposal"]
        assert prop["source"] == "rule_fallback"
        assert prop["error_code"] == "model_timeout"
        assert result.actual_status == "succeeded"

    def test_sensitive_tool_denied_not_executed(self):
        result = scenarios_mod.run_scenario("model_sensitive_tool_denied")
        if result.skipped:
            pytest.skip(f"WP-11 接口缺失：{result.skip_reason}")
        prop = result.extra["model_proposal"]
        assert prop["source"] == "rule_fallback"
        assert prop["error_code"] == "model_role_denied"
        assert result.tool_executions.get("mqtt.send_task", 0) == 0, (
            "敏感工具越权后 handler 不得执行"
        )


class TestReplayAndHandoff:
    def test_trace_replay_integrity(self):
        result = scenarios_mod.run_scenario("trace_replay_integrity")
        if result.skipped:
            pytest.skip(f"回放场景被跳过：{result.skip_reason}")
        rp = result.extra["replay"]
        assert rp["total_steps"] > 0
        assert rp["matched_steps"] == rp["total_steps"]

    def test_multi_role_handoff(self):
        result = scenarios_mod.run_scenario("multi_role_handoff")
        if result.skipped:
            pytest.skip(f"交接场景被跳过：{result.skip_reason}")
        assert result.extra["approval_handoff"] == {"attempted": 1, "succeeded": 1}
        assert result.tool_executions.get("mqtt.send_task", 0) == 1


class TestDeviceScenario:
    """WP-14 设备命令与故障注入：确定性 + 恢复率口径。"""

    def test_device_fault_recovery_rate(self):
        result = scenarios_mod.run_scenario("device_command_fault_injection")
        if result.skipped:
            pytest.skip(f"WP-14 接口缺失：{result.skip_reason}")
        dev = result.extra["device"]
        assert dev["faults_injected"] == 4
        assert dev["faults_recovered"] == 3
        assert dev["rate"] == 0.75
        rep = result.extra["device_report"]
        assert rep["deterministic_replay"] is True
        assert rep["no_fault_rate"] is None  # 无故障 → null（零分母语义）
        assert rep["commands_rejected"] >= 1  # 急停后 pause 被拒绝
        assert rep["acks_published"] >= 1


class TestSkipSemantics:
    """接口缺失时场景必须明确跳过并输出原因（不得伪造通过）。"""

    def test_forced_skip_on_missing_interface(self, monkeypatch):
        def fake_try_import(module_name):  # noqa: ANN001
            return None, f"{module_name} 缺失（测试强制）"

        monkeypatch.setattr(scenarios_mod, "_try_import", fake_try_import)
        for wid in (
            "persistent_restart_resume",
            "concurrent_idempotent_trigger",
            "stale_state_conflict",
            "model_valid_plan",
            "model_invalid_json_fallback",
            "model_timeout_fallback",
            "model_sensitive_tool_denied",
            "device_command_fault_injection",
        ):
            result = scenarios_mod.run_scenario(wid)
            assert result.skipped is True, f"{wid} 应在接口缺失时跳过"
            assert result.skip_reason and "缺失" in result.skip_reason
            assert result.passed is False

    def test_skip_results_excluded_from_metrics(self):
        """跳过场景不计入指标分母（compute_metrics 层面，纯逻辑）。"""
        from metrics import compute_metrics
        from scenarios import ScenarioResult

        skipped = ScenarioResult(
            scenario_id="model_valid_plan",
            passed=False,
            expected={"status": "succeeded", "error_code": None, "shape": "s"},
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
            skipped=True,
            skip_reason="接口缺失（测试）",
        )
        m = compute_metrics([skipped])
        assert m["denominators"]["scenarios"] == 0
        assert m["model_fallback_rate"] is None


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
