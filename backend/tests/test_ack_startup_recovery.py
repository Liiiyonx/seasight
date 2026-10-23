"""WP-14E 启动恢复接线测试 —— ACK 判重水位从 t_task_ack 启动恢复。

覆盖执行手册 WP-14E 要求：
1. **降级不失败**：数据库不可用 / 表不存在 / 查询异常 → warning 并继续
   启动，绝不抛异常（与 lifespan「每步独立 try」的降级风格一致）；
2. **幂等**：重复启动（重复调用恢复）不叠加、不报错；
3. **耗时上界**：恢复包在超时内，超时即降级，绝不让启动无限期挂起；
4. **可观测**：恢复结果（行数/耗时/是否降级）经返回 dict 与 ``/health``
   的 ``ack_recovery`` 字段可观测，不得静默；
5. **真实接线**：有可用 PostgreSQL 时在真实 scratch 库上验证
   ``recover_ack_tracker`` 从账本重建规范回执与设备 ACK 水位（幂等）；
   无数据库则 ``pytest.skip`` 并明确说明。

恢复边界（如实声明）：``duplicate`` / ``out_of_order`` 重启后可恢复；
``late`` 判定对「发布后未 ACK」的命令重启后仍不可恢复（内存登记表固有
边界，见 docs/mqtt-topics.md §5.5 与 docs/architecture.md）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.main import (  # noqa: E402
    ACK_TRACKER_REBUILD_TIMEOUT_SECONDS,
    create_app,
    recover_ack_tracker,
)
from app.mqtt.ack import AckEnvelope, AckResult, AckTracker  # noqa: E402


# ======================================================================
# 假会话 / 假工厂（纯内存，不触真实 DB）
# ======================================================================
class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self) -> list:
        return self._rows


class _FakeSession:
    """假 AsyncSession：可配置抛出异常或睡眠（测超时）。"""

    def __init__(self, rows: list | None = None, exc: Exception | None = None,
                 sleep: float = 0.0) -> None:
        self._rows = rows or []
        self._exc = exc
        self._sleep = sleep

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, stmt):
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._exc is not None:
            raise self._exc
        return _FakeResult(self._rows)


def _row(command_id: str, task_id: str, seq: int, outcome: str = "new") -> SimpleNamespace:
    """构造 t_task_ack 行的最小替身（rebuild_ack_tracker 读取的字段）。"""
    return SimpleNamespace(
        command_id=command_id,
        task_id=task_id,
        device_id="RBT-DEMO-01",
        seq=seq,
        received_at=datetime(2026, 9, 19, 8, 0, 0, tzinfo=timezone.utc),
        accepted=True,
        reason="dispatched",
        mode="navigating",
        outcome=outcome,
    )


# ======================================================================
# 1. 降级不失败
# ======================================================================
class TestDegradation:
    def test_db_down_degrades_and_continues(self) -> None:
        """数据库不可用（连接异常）→ 降级 warning 继续，不抛异常。"""
        tracker = AckTracker()

        def _raise():
            raise ConnectionError("connection refused (模拟数据库不可用)")

        def _factory():
            return _raise

        result = asyncio.run(recover_ack_tracker(tracker, session_factory=_factory))
        assert result["status"] == "degraded"
        assert result["rows"] == 0
        assert "ConnectionError" in result["detail"]
        assert result["elapsed_ms"] >= 0
        assert not tracker.is_acked("cmd_x"), "降级时不得污染 tracker"

    def test_table_missing_degrades(self) -> None:
        """表不存在（查询异常）→ 降级 warning 继续，不抛异常。"""
        tracker = AckTracker()
        session = _FakeSession(exc=RuntimeError("relation t_task_ack does not exist"))
        result = asyncio.run(
            recover_ack_tracker(tracker, session_factory=lambda: (lambda: session))
        )
        assert result["status"] == "degraded"
        assert "RuntimeError" in result["detail"]

    def test_tracker_none_degrades(self) -> None:
        """MQTT 客户端未启动（tracker=None）→ 降级，不抛异常。"""
        result = asyncio.run(recover_ack_tracker(None))
        assert result["status"] == "degraded"
        assert "ack_tracker" in result["detail"]


# ======================================================================
# 2. 耗时上界
# ======================================================================
class TestTimeBound:
    def test_timeout_bounds_recovery(self) -> None:
        """恢复超过上界 → 超时降级返回，绝不无限期挂起。"""
        tracker = AckTracker()
        slow = _FakeSession(sleep=0.5)          # 慢查询 0.5s
        import time as _time

        t0 = _time.monotonic()
        result = asyncio.run(
            recover_ack_tracker(
                tracker, session_factory=lambda: (lambda: slow), timeout=0.1
            )
        )
        elapsed = _time.monotonic() - t0
        assert result["status"] == "degraded"
        assert "上界" in result["detail"], "超时降级必须说明超时上界"
        # Windows 事件循环定时器可能有毫秒级提前回调，不能把下界卡在
        # 99ms；保留 50ms 下界用于排除“立即返回”，严格上界仍在下一条。
        assert result["elapsed_ms"] >= 0.1 * 1000 * 0.5, "不得瞬间跳过慢查询"
        assert elapsed < 2.0, "恢复不得远超超时上界"
        assert result["rows"] == 0

    def test_default_timeout_constant_positive(self) -> None:
        """默认超时上界必须为正数（启动不得无界挂起）。"""
        assert ACK_TRACKER_REBUILD_TIMEOUT_SECONDS > 0


# ======================================================================
# 3. 幂等 + 可观测
# ======================================================================
class TestIdempotencyAndObservability:
    def test_repeat_recovery_no_stacking(self) -> None:
        """重复启动（同一 tracker 重复恢复）不叠加、不报错。"""
        rows = [
            _row("cmd_restart_a", "tsk_restart", 5, outcome="new"),
            _row("cmd_restart_b", "tsk_restart", 3, outcome="out_of_order"),
        ]
        session = _FakeSession(rows=rows)
        tracker = AckTracker()

        first = asyncio.run(
            recover_ack_tracker(tracker, session_factory=lambda: (lambda: session))
        )
        second = asyncio.run(
            recover_ack_tracker(tracker, session_factory=lambda: (lambda: session))
        )

        # 行数不叠加
        assert first["rows"] == second["rows"] == 2
        # 语义不叠加：重复回执仍判 duplicate；设备水位取 max 而非求和
        assert tracker.is_acked("cmd_restart_a")
        assert tracker.ack_for("cmd_restart_a") is not None
        dup = AckEnvelope(
            command_id="cmd_restart_a", task_id="tsk_restart",
            device_id="RBT-DEMO-01", seq=5, received_at=100.0,
            accepted=True, reason="", mode="",
        )
        assert tracker.classify(dup) == AckResult.DUPLICATE
        # 低于已恢复水位（max seq=5）的新命令 → out_of_order
        low = AckEnvelope(
            command_id="cmd_restart_x", task_id="tsk_restart",
            device_id="RBT-DEMO-01", seq=4, received_at=100.0,
            accepted=True, reason="", mode="",
        )
        assert tracker.classify(low) == AckResult.OUT_OF_ORDER
        assert first["status"] == second["status"] == "ok"

    def test_observable_result_fields(self) -> None:
        """恢复结果以可观测字段返回（status/rows/elapsed_ms/detail）。"""
        session = _FakeSession(rows=[_row("cmd_obs", "tsk_obs", 1)])
        result = asyncio.run(
            recover_ack_tracker(AckTracker(), session_factory=lambda: (lambda: session))
        )
        assert result["status"] == "ok"
        assert result["rows"] == 1
        assert result["elapsed_ms"] >= 0
        assert result["detail"]

    def test_health_reports_ack_recovery_field(self) -> None:
        """/health 顶层暴露 ack_recovery 字段（可观测），且不破坏依赖明细。"""
        from contextlib import asynccontextmanager

        from fastapi.testclient import TestClient

        app = create_app()

        @asynccontextmanager
        async def _no_lifespan(_app):
            yield

        app.router.lifespan_context = _no_lifespan   # 不跑真实启动副作用
        with TestClient(app) as client:
            body = client.get("/health").json()
            assert "ack_recovery" in body, "health 必须暴露 ack_recovery 字段"
            assert body["ack_recovery"] is None, "未恢复时为 None，不撒谎"
            assert set(body["dependencies"]) == {"redis", "mqtt", "database"}

            # 设置恢复结果后必须如实上报（可观测，不静默）
            app.state.ack_tracker_recovery = {
                "status": "ok",
                "rows": 3,
                "elapsed_ms": 1.25,
                "detail": "从 t_task_ack 重建 3 条规范回执",
            }
            body2 = client.get("/health").json()
            assert body2["ack_recovery"]["status"] == "ok"
            assert body2["ack_recovery"]["rows"] == 3
            assert body2["ack_recovery"]["elapsed_ms"] == 1.25
            if client.portal is not None:
                from app.db.session import dispose_engine

                client.portal.call(dispose_engine)


# ======================================================================
# 4. 真实库接线（scratch PostgreSQL）
# ======================================================================
_ENV_KEYS = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
_saved_env: dict[str, str | None] | None = None


def _db_params() -> dict:
    from app.core.config import settings

    return {
        "host": settings.postgres_host,
        "port": settings.postgres_port,
        "dbname": settings.postgres_db,
        "user": settings.postgres_user,
        "password": settings.postgres_password,
    }


def _db_reachable(params: dict) -> bool:
    import psycopg2

    try:
        conn = psycopg2.connect(
            host=params["host"], port=params["port"], dbname=params["dbname"],
            user=params["user"], password=params["password"], connect_timeout=3,
        )
        conn.close()
        return True
    except Exception:
        return False


def _point_settings_at_db(dbname: str, params: dict) -> None:
    global _saved_env
    if _saved_env is None:
        _saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["POSTGRES_HOST"] = str(params["host"])
    os.environ["POSTGRES_PORT"] = str(params["port"])
    os.environ["POSTGRES_DB"] = dbname
    os.environ["POSTGRES_USER"] = str(params["user"])
    os.environ["POSTGRES_PASSWORD"] = str(params["password"])
    import app.core.config as cfg_mod

    cfg_mod.get_settings.cache_clear()
    importlib.reload(cfg_mod)


def _restore_settings() -> None:
    global _saved_env
    if _saved_env is not None:
        for k, v in _saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _saved_env = None
    import app.core.config as cfg_mod

    cfg_mod.get_settings.cache_clear()
    importlib.reload(cfg_mod)


def _query_rows(params: dict, sql: str) -> list[tuple]:
    import psycopg2

    conn = psycopg2.connect(
        host=params["host"], port=params["port"], dbname=params["dbname"],
        user=params["user"], password=params["password"], connect_timeout=5,
    )
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return list(cur.fetchall())
    finally:
        conn.close()


def _execute_sql(params: dict, sql: str) -> None:
    import psycopg2

    conn = psycopg2.connect(
        host=params["host"], port=params["port"], dbname=params["dbname"],
        user=params["user"], password=params["password"], connect_timeout=5,
    )
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(sql)
    finally:
        conn.close()


def _drop_scratch(admin: dict, scratch: str) -> None:
    import psycopg2

    try:
        a = psycopg2.connect(**admin, connect_timeout=3)
        a.autocommit = True
        c = a.cursor()
        c.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{scratch}'")
        c.execute(f"DROP DATABASE IF EXISTS {scratch}")
        a.close()
    except Exception:
        pass


def _create_scratch(admin: dict, scratch: str) -> None:
    import psycopg2

    a = psycopg2.connect(**admin, connect_timeout=3)
    a.autocommit = True
    c = a.cursor()
    c.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{scratch}'")
    c.execute(f"DROP DATABASE IF EXISTS {scratch}")
    c.execute(f"CREATE DATABASE {scratch}")
    a.close()


def _scratch_admin(params: dict) -> dict:
    import psycopg2

    admin = {**params, "dbname": "postgres"}
    try:
        psycopg2.connect(**admin, connect_timeout=3).close()
    except Exception:
        admin = dict(params)
    return admin


@pytest.fixture(scope="module")
def startup_db():
    """真实 scratch 库（模块级）：01_schema.sql 幂等建表 → 供启动恢复接线验证。

    无可用 PostgreSQL 则跳过并明确说明；本机有 PostgreSQL 17，真实执行。
    """
    params = _db_params()
    if not _db_reachable(params):
        pytest.skip("本机无可用 PostgreSQL/PostGIS —— 跳过启动恢复真实库验证；降级/幂等/超时/可观测已由纯内存测试覆盖")
    admin = _scratch_admin(params)
    scratch = f"seasight_wp14e_startup_{os.getpid()}"
    engine = None
    try:
        _create_scratch(admin, scratch)
        _execute_sql(
            {**params, "dbname": scratch},
            Path(BACKEND / "db" / "init" / "01_schema.sql").read_text(encoding="utf-8"),
        )
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from sqlalchemy.pool import NullPool

        url = (
            f"postgresql+asyncpg://{params['user']}:{params['password']}"
            f"@{params['host']}:{params['port']}/{scratch}"
        )
        engine = create_async_engine(url, poolclass=NullPool)
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        yield factory
    finally:
        if engine is not None:
            asyncio.run(engine.dispose())
        _restore_settings()
        _drop_scratch(admin, scratch)


@pytest.fixture
def _wipe(startup_db):
    yield

    async def _do() -> None:
        from sqlalchemy import text

        async with startup_db() as session:
            await session.execute(text("DELETE FROM t_task_ack"))
            await session.commit()

    asyncio.run(_do())


def _seed_task(factory, task_id: str) -> None:
    async def _do() -> None:
        from app.models.task import Task

        async with factory() as session:
            session.add(
                Task(
                    task_id=task_id,
                    target_location="SRID=4326;POINT(119.6 26.3)",
                    status="assigned",
                    robot_id="RBT-DEMO-01",
                    township="马鼻镇",
                )
            )
            await session.commit()

    asyncio.run(_do())


class TestRealStartupRecovery:
    """真实 scratch 库上：recover_ack_tracker 从账本重建判重水位，且幂等。"""

    pytestmark = pytest.mark.usefixtures("_wipe")

    def test_rebuilds_watermark_and_idempotent(self, startup_db) -> None:
        from app.repositories import TaskAckRepository

        _seed_task(startup_db, "tsk_startup_001")

        async def _seed_ledger() -> None:
            async with startup_db() as session:
                for command_id, seq, outcome in (
                    ("cmd_st_1", 5, "new"),
                    ("cmd_st_2", 3, "out_of_order"),
                ):
                    await TaskAckRepository(session).record_arrival(
                        command_id=command_id,
                        task_id="tsk_startup_001",
                        device_id="RBT-DEMO-01",
                        seq=seq,
                        outcome=outcome,
                        accepted=True,
                        reason="dispatched",
                        mode="navigating",
                        received_at=datetime(2026, 9, 19, 8, 0, 0, tzinfo=timezone.utc),
                        raw_payload={"command_id": command_id},
                    )
                await session.commit()

        asyncio.run(_seed_ledger())

        tracker = AckTracker()
        factory = startup_db

        # 启动恢复：真实库重建
        first = asyncio.run(recover_ack_tracker(tracker, session_factory=lambda: factory))
        assert first["status"] == "ok"
        assert first["rows"] == 2
        assert tracker.is_acked("cmd_st_1")
        assert tracker.is_acked("cmd_st_2")

        # 恢复的 duplicate 语义
        dup = AckEnvelope(
            command_id="cmd_st_1", task_id="tsk_startup_001", device_id="RBT-DEMO-01",
            seq=5, received_at=100.0, accepted=True, reason="", mode="",
        )
        assert tracker.classify(dup) == AckResult.DUPLICATE
        # 设备水位恢复（max seq=5）：seq=4 新命令 → out_of_order
        low = AckEnvelope(
            command_id="cmd_st_x", task_id="tsk_startup_001", device_id="RBT-DEMO-01",
            seq=4, received_at=100.0, accepted=True, reason="", mode="",
        )
        assert tracker.classify(low) == AckResult.OUT_OF_ORDER

        # 幂等：第二次启动恢复不叠加、不报错
        second = asyncio.run(recover_ack_tracker(tracker, session_factory=lambda: factory))
        assert second["status"] == "ok"
        assert second["rows"] == 2
        assert tracker.classify(dup) == AckResult.DUPLICATE
        assert tracker.classify(low) == AckResult.OUT_OF_ORDER
