"""WP-14E 孪生闭环端到端联调用例（无真实 Broker，进程内注入构造）。

串起一条可复现的本机闭环：
    edge/device_sim 生成冻结 ACK 信封
        → 平台 handle_robot_ack（校验与判定 → 写账本 → 状态推进 → 提交）
        → 任务状态推进 + t_task_ack 落账本
        → 模拟进程重启（新建 AckTracker + rebuild_ack_tracker 从账本恢复）
        → 重放同一回执：任务**不再**重复推进，账本 duplicate_count 递增且
          首次规范回执（raw_payload）不被覆盖。

负例（按 WP-14C 语义）：
- 身份不一致（payload.device_id / topic robot_id / task.robot_id 三者失配）
  → 整条拒绝，不污染 tracker、不写账本；
- 未知任务 → 拒绝，不污染 tracker、不写账本；
- 未登记 deadline 的命令 → 仍可正常接收，但**不得伪判 late**
  （classify 为 new，账本 outcome 为 new 而非 late）。

★ 数据库策略：有可用 PostgreSQL 时走真实 scratch 库（01_schema.sql 幂等建
  表 + 真实 AsyncSession + 真实 DispatchEngine + 真实 TaskAckRepository）；
  无数据库则 pytest.skip 并明确说明 —— 绝不把内存假设冒充实库结论。
  孪生侧仅用内存 transport + 假时钟，不依赖真实网络 / broker。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
EDGE = BACKEND.parent / "edge"
if str(EDGE) not in sys.path:
    sys.path.insert(0, str(EDGE))

from app.models.task import Task  # noqa: E402
from app.models.task_ack import TaskAck  # noqa: E402,F401
from app.mqtt.ack import AckResult, AckTracker  # noqa: E402
from app.mqtt.handlers import handle_robot_ack  # noqa: E402
from app.repositories import TaskAckRepository  # noqa: E402
from device_sim.device import DeviceTwin, ack_topic  # noqa: E402
from device_sim.protocol import FakeClock, SequentialIdFactory  # noqa: E402
from device_sim.transport import MemoryTransport  # noqa: E402

TWIN_DEVICE = "RBT-TWIN-01"
TWIN_TARGET = {"lng": 119.66, "lat": 26.39}


def twin_ack(
    device_id: str,
    command_id: str,
    seq: int,
    task_id: str,
    *,
    clock_start: float = 100.0,
    ttl: float = 30.0,
) -> tuple[dict, str]:
    """用 edge/device_sim 生成一条冻结 ACK 信封（内存 transport，无网络）。

    注入一条 dispatch 命令并 tick，取孪生发布的回执报文与其主题。
    """
    transport = MemoryTransport()
    twin = DeviceTwin(
        device_id,
        transport,
        clock=FakeClock(clock_start),
        id_factory=SequentialIdFactory("ack"),
    )
    twin.receive_command(
        {
            "command_id": command_id,
            "device_id": device_id,
            "seq": seq,
            "issued_at": clock_start,
            "expires_at": clock_start + ttl,
            "action": "dispatch",
            "params": {"task_id": task_id, "target": TWIN_TARGET},
        }
    )
    twin.tick()
    acks = [p for t, p, q in transport.delivered if t == ack_topic(device_id)]
    assert len(acks) == 1, "孪生应发布恰好一条回执"
    return acks[0], f"robot/{device_id}/cmd/ack"


# ======================================================================
# 真实 scratch 库（模块级）
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
def twin_db():
    """真实 scratch 库（模块级）：01_schema.sql ×2 幂等建表。

    无可用 PostgreSQL 则跳过并明确说明；本机有 PostgreSQL 17，真实执行。
    """
    params = _db_params()
    if not _db_reachable(params):
        pytest.skip("本机无可用 PostgreSQL/PostGIS —— 跳过孪生闭环真实库验证（内存负例仍执行）")
    admin = _scratch_admin(params)
    scratch = f"seasight_wp14e_twin_{os.getpid()}"
    engine = None
    try:
        _create_scratch(admin, scratch)
        schema = Path(BACKEND / "db" / "init" / "01_schema.sql").read_text(encoding="utf-8")
        _execute_sql({**params, "dbname": scratch}, schema)   # 幂等第 1 次
        _execute_sql({**params, "dbname": scratch}, schema)   # 幂等第 2 次
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
def _wipe_twin(twin_db):
    yield

    async def _do() -> None:
        from sqlalchemy import text

        async with twin_db() as session:
            await session.execute(text("DELETE FROM t_task_ack"))
            await session.commit()

    asyncio.run(_do())


def _seed_assigned_task(factory, task_id: str, *, robot_id: str = TWIN_DEVICE) -> None:
    async def _do() -> None:
        async with factory() as session:
            session.add(
                Task(
                    task_id=task_id,
                    target_location="SRID=4326;POINT(119.6 26.3)",
                    status="assigned",
                    robot_id=robot_id,
                    township="马鼻镇",
                )
            )
            await session.commit()

    asyncio.run(_do())


def _task_status(factory, task_id: str) -> str | None:
    async def _do() -> str | None:
        from sqlalchemy import select as _select

        async with factory() as session:
            task = (
                await session.execute(_select(Task).where(Task.task_id == task_id))
            ).scalar_one_or_none()
            return task.status if task is not None else None

    return asyncio.run(_do())


def _ledger_row(factory, command_id: str) -> TaskAck | None:
    async def _do() -> TaskAck | None:
        from sqlalchemy import select as _select

        async with factory() as session:
            return (
                await session.execute(
                    _select(TaskAck).where(TaskAck.command_id == command_id)
                )
            ).scalar_one_or_none()

    return asyncio.run(_do())


def _rebuild(tracker: AckTracker, factory) -> int:
    async def _do() -> int:
        async with factory() as session:
            return await TaskAckRepository(session).rebuild_ack_tracker(tracker)

    return asyncio.run(_do())


# ======================================================================
# 主闭环：孪生 ACK → 平台消费 → 推进 + 落账 → 重启 → 重放不重复推进
# ======================================================================
class TestTwinLoopback:
    pytestmark = pytest.mark.usefixtures("_wipe_twin")

    def test_loopback_restart_replay_no_double_advance(self, twin_db) -> None:
        """端到端：孪生回执 → handle_robot_ack → 推进+落账；重启重建后重放
        同一回执 → 不重复推进、duplicate_count 递增、首次规范回执不被覆盖。"""
        _seed_assigned_task(twin_db, "tsk_twin_001")
        ack_payload, topic = twin_ack(
            TWIN_DEVICE, "cmd_tsk_twin_001", seq=1, task_id="tsk_twin_001"
        )

        # ---- 首次：孪生回执 → 平台消费（new + 推进 + 落账）----
        tracker = AckTracker()
        first = asyncio.run(
            handle_robot_ack(
                topic, ack_payload,
                tracker=tracker, session_factory=lambda: twin_db,
            )
        )
        assert first == AckResult.NEW
        assert _task_status(twin_db, "tsk_twin_001") == "navigating", "首次回执应推进任务"
        row1 = _ledger_row(twin_db, "cmd_tsk_twin_001")
        assert row1 is not None
        assert row1.outcome == "new"
        assert row1.accepted is True
        assert row1.duplicate_count == 0
        assert row1.raw_payload == ack_payload, "首次规范回执原文留档"

        # ---- 模拟进程重启：全新 AckTracker + 从账本重建判重水位 ----
        restarted = AckTracker()
        assert _rebuild(restarted, twin_db) == 1
        assert restarted.is_acked("cmd_tsk_twin_001"), "重启后 duplicate 判定应恢复"

        # ---- 重放同一回执：不重复推进，只累计账本 ----
        replay = asyncio.run(
            handle_robot_ack(
                topic, ack_payload,
                tracker=restarted, session_factory=lambda: twin_db,
            )
        )
        assert replay == AckResult.DUPLICATE
        assert _task_status(twin_db, "tsk_twin_001") == "navigating", "重放不得重复推进"
        row2 = _ledger_row(twin_db, "cmd_tsk_twin_001")
        assert row2 is not None
        assert row2.duplicate_count == 1, "账本重复次数应递增"
        assert row2.raw_payload == ack_payload, "首次规范回执不得被覆盖"

    def test_restart_without_rebuild_ledger_fallback_blocks_advance(self, twin_db) -> None:
        """重启后即使不重建 tracker，账本兜底（record_arrival 判 duplicate）
        同样阻止重复推进 —— 账本是权威，不依赖内存重建。"""
        _seed_assigned_task(twin_db, "tsk_twin_002")
        ack_payload, topic = twin_ack(
            TWIN_DEVICE, "cmd_tsk_twin_002", seq=1, task_id="tsk_twin_002"
        )
        tracker = AckTracker()
        assert asyncio.run(
            handle_robot_ack(topic, ack_payload, tracker=tracker, session_factory=lambda: twin_db)
        ) == AckResult.NEW
        assert _task_status(twin_db, "tsk_twin_002") == "navigating"

        # 全新 tracker（未重建）：重放 → 内存判 new，但账本已有规范行 →
        # record_arrival 返回 duplicate → 不推进
        fresh = AckTracker()
        replay = asyncio.run(
            handle_robot_ack(topic, ack_payload, tracker=fresh, session_factory=lambda: twin_db)
        )
        assert replay == AckResult.DUPLICATE
        assert _task_status(twin_db, "tsk_twin_002") == "navigating"
        assert _ledger_row(twin_db, "cmd_tsk_twin_002").duplicate_count == 1  # type: ignore[union-attr]


# ======================================================================
# 负例：身份不一致 / 未知任务 / 未登记 deadline
# ======================================================================
class TestNegativeCases:
    pytestmark = pytest.mark.usefixtures("_wipe_twin")

    def test_identity_mismatch_rejected_no_pollution(self, twin_db) -> None:
        """payload.device_id / topic robot_id / task.robot_id 不一致 → 整条拒绝，
        不污染 tracker、不写账本（WP-14C 身份三重校验）。"""
        _seed_assigned_task(twin_db, "tsk_twin_003")
        ack_payload, topic = twin_ack(
            TWIN_DEVICE, "cmd_tsk_twin_003", seq=1, task_id="tsk_twin_003"
        )

        # (a) payload.device_id 与 topic / task.robot_id 不一致
        evil_payload = {**ack_payload, "device_id": "RBT-EVIL-01"}
        tracker = AckTracker()
        result = asyncio.run(
            handle_robot_ack(topic, evil_payload, tracker=tracker, session_factory=lambda: twin_db)
        )
        assert result is None, "身份不一致必须整条拒绝"
        assert _task_status(twin_db, "tsk_twin_003") == "assigned", "不得推进"
        assert not tracker.is_acked("cmd_tsk_twin_003"), "不得污染 tracker"
        assert all(v == 0 for v in tracker.counts.values())
        assert _ledger_row(twin_db, "cmd_tsk_twin_003") is None, "不得写账本"

        # (b) topic robot_id 与 payload / task.robot_id 不一致
        wrong_topic = "robot/RBT-EVIL-01/cmd/ack"
        tracker2 = AckTracker()
        result2 = asyncio.run(
            handle_robot_ack(wrong_topic, ack_payload, tracker=tracker2, session_factory=lambda: twin_db)
        )
        assert result2 is None
        assert _task_status(twin_db, "tsk_twin_003") == "assigned"
        assert not tracker2.is_acked("cmd_tsk_twin_003")
        assert _ledger_row(twin_db, "cmd_tsk_twin_003") is None

    def test_unknown_task_rejected_no_pollution(self, twin_db) -> None:
        """未知任务（账本与任务表都无）→ 拒绝，不污染 tracker、不写账本。"""
        ack_payload, topic = twin_ack(
            TWIN_DEVICE, "cmd_tsk_nonexistent", seq=1, task_id="tsk_nonexistent"
        )
        tracker = AckTracker()
        result = asyncio.run(
            handle_robot_ack(topic, ack_payload, tracker=tracker, session_factory=lambda: twin_db)
        )
        assert result is None
        assert not tracker.is_acked("cmd_tsk_nonexistent")
        assert all(v == 0 for v in tracker.counts.values())
        assert _ledger_row(twin_db, "cmd_tsk_nonexistent") is None, "未知任务不得写账本"

    def test_unregistered_deadline_not_falsely_late(self, twin_db) -> None:
        """未登记 deadline 的命令仍可正常接收，但不得伪判 late（WP-14C 回落
        策略）：classify 为 new，账本 outcome 为 new 而非 late。"""
        _seed_assigned_task(twin_db, "tsk_twin_late")
        # received_at(1000.0) 远超任何 expires_at，但 tracker 从未登记该命令
        payload = {
            "ack_id": "ack_late_001",
            "command_id": "cmd_tsk_twin_late",
            "device_id": TWIN_DEVICE,
            "seq": 9,
            "received_at": 1000.0,
            "accepted": True,
            "reason": "dispatched",
            "mode": "navigating",
        }
        tracker = AckTracker()
        assert not tracker.has_deadline("cmd_tsk_twin_late")
        assert tracker.classify(parse_envelope(payload)) == AckResult.NEW, "未登记不得伪判 late"

        result = asyncio.run(
            handle_robot_ack(
                f"robot/{TWIN_DEVICE}/cmd/ack", payload,
                tracker=tracker, session_factory=lambda: twin_db,
            )
        )
        assert result == AckResult.NEW
        assert _task_status(twin_db, "tsk_twin_late") == "navigating"
        row = _ledger_row(twin_db, "cmd_tsk_twin_late")
        assert row is not None
        assert row.outcome == "new", f"账本 outcome 必须是 new，实际 {row.outcome}（不得伪判 late）"


def parse_envelope(payload: dict):
    from app.mqtt.ack import parse_ack_envelope

    env = parse_ack_envelope(payload)
    assert env is not None
    return env
