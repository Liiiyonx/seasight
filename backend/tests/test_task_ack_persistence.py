"""WP-14D ACK 持久化、迁移与重启恢复 —— t_task_ack 审计账本测试。

覆盖四层：
1. ORM 字段/约束/索引与冻结契约对账（纯内存，无数据库依赖）。
2. backend/db/init/01_schema.sql 幂等 DDL 与契约对账（正则/AST 静态解析）。
3. Alembic 迁移 20260919_1700_task_ack：
   - 静态一致性（AST：只建/只删 t_task_ack、revision 链正确且 <=32 字符、
     迁移可导入 ORM、ScriptDirectory 单一 head）；
   - 真实数据库迁移（本机有可用 PostgreSQL/PostGIS 则创建独立 scratch 库：
     01_schema.sql ×2 幂等 → 模拟存量库 → stamp 1600 → upgrade head →
     校验列/唯一/外键/索引 → 冒烟写读 → downgrade 1600 → 校验 → 清理；
     无数据库则跳过并说明，静态对账已执行）。
4. TaskAckRepository 行为（真实 scratch PostgreSQL 的异步会话）：
   - 首次到达建规范行（outcome 取 WP-14C 判定、raw_payload 留档、
     duplicate_count=0、received_at 与 received_wall_at 分离）；
   - 重复到达只累计（duplicate_count+1、last_duplicate_at/last_payload 更新、
     raw_payload 与首次判定不被覆盖、不产生第二条规范行）；
   - 并发唯一冲突转 duplicate（模拟 SELECT 未命中窗口 + 真实撞唯一键 →
     回滚重查 → 累计，无第二条规范行）；
   - AckTracker 重启恢复（rebuild_ack_tracker：规范回执 + 设备 ACK 水位，
     duplicate / out_of_order 判定重启后可恢复）；
   - 分页查询按 received_wall_at DESC。

★ 数据库可用性：本机有 PostgreSQL 17（seasight 库可达），仓储与迁移测试
  全部走真实 scratch 库；若无数据库则 pytest.skip 并明确说明，
  绝不把静态检查冒充实库验证。
"""

from __future__ import annotations

import asyncio
import ast
import importlib.util
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.db.session import Base  # noqa: E402
import app.models.task_ack  # noqa: E402,F401  注册 t_task_ack 到 Base.metadata
from app.models import TaskAck  # noqa: E402
from app.models.task import Task  # noqa: E402
from app.mqtt.ack import AckEnvelope, AckResult, AckTracker  # noqa: E402
from app.repositories import TaskAckRepository  # noqa: E402

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL_PATH = BACKEND_ROOT / "db" / "init" / "01_schema.sql"
ALEMBIC_VERSIONS_DIR = BACKEND_ROOT / "alembic" / "versions"
ALEMBIC_DIR = BACKEND_ROOT / "alembic"

BASELINE_REVISION = "20260918_1000_baseline"
AGENT_STATE_REVISION = "20260919_1600_agent_state"
TASK_ACK_REVISION = "20260919_1700_task_ack"
APPROVER_ROLE_REVISION = "20260919_1800_approver_role"
KNOWLEDGE_REVISION = "20260919_1900_knowledge_assets"
# 对话助手迁移接在知识域之后，是当前唯一 head。
CHAT_SESSION_REVISION = "20260922_1000_chat_session"

KNOWLEDGE_TABLES = (
    "t_decision_evidence",
    "t_decision_trace",
    "t_ontology_relation",
    "t_ontology_node",
    "t_ontology_version",
    "t_knowledge_asset_version",
    "t_knowledge_asset",
)
# 对话助手两表；删除顺序按依赖逆序（message 引用 session，先删 message）。
CHAT_TABLES = (
    "t_chat_message",
    "t_chat_session",
)

# ---------- 冻结契约（WP-14D） ----------
TASK_ACK_COLUMNS: set[str] = {
    "id", "command_id", "task_id", "device_id", "seq", "outcome", "accepted",
    "reason", "mode", "received_at", "received_wall_at", "duplicate_count",
    "last_duplicate_at", "raw_payload", "last_payload", "created_at", "updated_at",
}
TASK_ACK_INDEXES: set[str] = {
    "idx_task_ack_task", "idx_task_ack_device_seq", "idx_task_ack_outcome",
}
EXISTING_BUSINESS_TABLES = (
    "t_device", "t_event", "t_task", "t_track", "t_report_daily", "t_user", "t_audit_log",
)


# ============================================================
# 1. ORM 契约对账
# ============================================================
class TestOrmContract:
    def test_model_registered_in_metadata(self) -> None:
        assert "t_task_ack" in Base.metadata.tables

    def test_columns_match_frozen_contract(self) -> None:
        table = Base.metadata.tables["t_task_ack"]
        assert set(table.columns.keys()) == TASK_ACK_COLUMNS

    def test_command_id_unique_constraint(self) -> None:
        table = Base.metadata.tables["t_task_ack"]
        uniq = {
            frozenset(c.columns.keys())
            for c in table.constraints
            if isinstance(c, UniqueConstraint)
        }
        assert frozenset({"command_id"}) in uniq, "command_id 必须唯一（一命令一条规范回执）"

    def test_fk_to_task_id_cascade(self) -> None:
        table = Base.metadata.tables["t_task_ack"]
        fks = [c for c in table.constraints if isinstance(c, ForeignKeyConstraint)]
        assert fks, "t_task_ack 缺外键"
        targets = {c.elements[0].target_fullname for c in fks}
        assert "t_task.task_id" in targets, "外键必须关联 t_task.task_id"
        ondelete = {c.ondelete for c in fks}
        assert "CASCADE" in ondelete, "外键必须 ON DELETE CASCADE"

    def test_frozen_indexes_declared(self) -> None:
        table = Base.metadata.tables["t_task_ack"]
        names = {ix.name for ix in table.indexes}
        assert TASK_ACK_INDEXES <= names, f"缺少冻结索引：{TASK_ACK_INDEXES - names}"


# ============================================================
# 2. 01_schema.sql 契约与幂等性（静态解析）
# ============================================================
_TYPE_RE = re.compile(
    r"^\s*([a-z_][a-z0-9_]*)\s+"
    r"(BIGSERIAL|VARCHAR|TEXT|TIMESTAMPTZ|NUMERIC|INTEGER|INT|BIGINT|BOOLEAN|JSONB|SMALLINT)\b",
    re.IGNORECASE,
)
_TABLE_BLOCK_RE = re.compile(
    r"CREATE TABLE IF NOT EXISTS (t_[a-z0-9_]+)\s*\((.*?)\);", re.S | re.IGNORECASE
)


def _parse_table_blocks(sql: str) -> dict[str, set[str]]:
    blocks: dict[str, set[str]] = {}
    for m in _TABLE_BLOCK_RE.finditer(sql):
        name = m.group(1)
        cols: set[str] = set()
        for line in m.group(2).splitlines():
            tm = _TYPE_RE.match(line)
            if tm:
                cols.add(tm.group(1))
        blocks[name] = cols
    return blocks


def _schema_sql() -> str:
    return SCHEMA_SQL_PATH.read_text(encoding="utf-8")


class TestSchemaSqlContract:
    def test_schema_has_task_ack_with_contract_columns(self) -> None:
        blocks = _parse_table_blocks(_schema_sql())
        assert "t_task_ack" in blocks
        assert blocks["t_task_ack"] == TASK_ACK_COLUMNS, (
            "01_schema.sql 中 t_task_ack 字段与冻结契约不一致"
        )

    def test_schema_idempotent_and_preserves_existing(self) -> None:
        sql = _schema_sql()
        assert "CREATE TABLE IF NOT EXISTS t_task_ack " in sql
        # 唯一约束
        assert "CONSTRAINT uq_task_ack_command_id UNIQUE (command_id)" in sql
        # 外键 CASCADE
        assert "REFERENCES t_task(task_id) ON DELETE CASCADE" in sql
        # 三条冻结索引全部 IF NOT EXISTS
        for idx in TASK_ACK_INDEXES:
            assert re.search(rf"CREATE INDEX IF NOT EXISTS {idx}\s", sql), idx
        # 既有业务表原样保留（仍是 IF NOT EXISTS）
        for t in EXISTING_BUSINESS_TABLES:
            assert f"CREATE TABLE IF NOT EXISTS {t} " in sql


# ============================================================
# 3. Alembic 迁移：静态一致性
# ============================================================
def _migration_file() -> Path:
    matches = sorted(ALEMBIC_VERSIONS_DIR.glob("*_task_ack.py"))
    assert len(matches) == 1, f"backend/alembic/versions/ 下应恰有一个 *_task_ack.py，实际: {matches}"
    return matches[0]


def _migration_ast() -> ast.Module:
    return ast.parse(_migration_file().read_text(encoding="utf-8"))


def _migration_create_tables(tree: ast.Module) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_table"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            cols: set[str] = set()
            for arg in node.args[1:]:
                if (
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Attribute)
                    and arg.func.attr == "Column"
                    and arg.args
                    and isinstance(arg.args[0], ast.Constant)
                ):
                    cols.add(arg.args[0].value)
            tables[node.args[0].value] = cols
    return tables


def _migration_drop_tables(tree: ast.Module) -> set[str]:
    dropped: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "drop_table"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            dropped.add(node.args[0].value)
    return dropped


class TestMigrationStatic:
    def test_migration_identity_and_chain(self) -> None:
        namespace: dict = {"__file__": str(_migration_file())}
        exec(compile(_migration_file().read_text(encoding="utf-8"), str(_migration_file()), "exec"), namespace)
        assert namespace["revision"] == TASK_ACK_REVISION
        assert namespace["down_revision"] == AGENT_STATE_REVISION
        assert len(TASK_ACK_REVISION) <= 32, "revision id 不得超过 alembic_version.version_num VARCHAR(32)"

    def test_migration_imports_orm_for_autogenerate(self) -> None:
        src = _migration_file().read_text(encoding="utf-8")
        assert "import app.models.task_ack" in src, "迁移应导入 ORM，使 t_task_ack 注册到 Base.metadata"

    def test_migration_upgrade_creates_only_task_ack(self) -> None:
        tree = _migration_ast()
        created = _migration_create_tables(tree)
        assert set(created.keys()) == {"t_task_ack"}
        assert created["t_task_ack"] == TASK_ACK_COLUMNS, "迁移字段与冻结契约不一致"

    def test_migration_downgrade_drops_only_task_ack(self) -> None:
        tree = _migration_ast()
        dropped = _migration_drop_tables(tree)
        assert dropped == {"t_task_ack"}
        assert not dropped.intersection(set(EXISTING_BUSINESS_TABLES))

    def test_alembic_script_directory_chain(self) -> None:
        from alembic.script import ScriptDirectory

        cfg = _alembic_config()
        script = ScriptDirectory.from_config(cfg)
        assert script.get_heads() == [CHAT_SESSION_REVISION], "迁移链应保持单一 head"
        ack = script.get_revision(TASK_ACK_REVISION)
        assert ack.down_revision == AGENT_STATE_REVISION, "1700 必须从 1600_agent_state 线性升级"
        approver = script.get_revision(APPROVER_ROLE_REVISION)
        assert approver.down_revision == TASK_ACK_REVISION, "1800 必须从 1700_task_ack 线性升级"
        knowledge = script.get_revision(KNOWLEDGE_REVISION)
        assert (
            knowledge.down_revision == APPROVER_ROLE_REVISION
        ), "1900 必须从 1800_approver_role 线性升级"
        chat = script.get_revision(CHAT_SESSION_REVISION)
        assert (
            chat.down_revision == KNOWLEDGE_REVISION
        ), "20260922 必须从 1900_knowledge_assets 线性升级"


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(ALEMBIC_DIR.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    return cfg


# ============================================================
# 数据库辅助（scratch 库）
# ============================================================
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
    """把 app.core.config.settings 指向指定库（Alembic env.py 会读它）。"""
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
    """执行不返回结果集的语句（DDL 等）。"""
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


def _db_tables(params: dict) -> set[str]:
    return {
        r[0]
        for r in _query_rows(
            params,
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'",
        )
    }


def _drop_scratch(admin: dict, scratch: str) -> None:
    import psycopg2

    try:
        a = psycopg2.connect(**admin, connect_timeout=3)
        a.autocommit = True
        c = a.cursor()
        c.execute(
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{scratch}'"
        )
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


def _apply_schema_sql(dbname: str, params: dict) -> None:
    """在指定库执行 01_schema.sql 两次（幂等）。"""
    import psycopg2

    s = psycopg2.connect(**{**params, "dbname": dbname}, connect_timeout=3)
    s.autocommit = True
    cur = s.cursor()
    sql = _schema_sql()
    cur.execute(sql)
    cur.execute(sql)
    s.close()


def _scratch_admin(params: dict) -> dict:
    import psycopg2

    admin = {**params, "dbname": "postgres"}
    try:
        psycopg2.connect(**admin, connect_timeout=3).close()
    except Exception:
        admin = dict(params)
    return admin


@pytest.fixture(scope="module")
def ack_db():
    """真实 scratch 库（模块级）：01_schema.sql ×2 幂等 → 模拟存量（drop t_task_ack）
    → stamp 1600 → upgrade head（应用 1700 与后续 head）→ 供仓储行为测试使用。

    无可用 PostgreSQL 则跳过；本机有 PostgreSQL 17，真实执行。
    """
    params = _db_params()
    if not _db_reachable(params):
        pytest.skip("本机无可用 PostgreSQL/PostGIS —— 跳过真实仓储测试；静态契约对账已执行")
    admin = _scratch_admin(params)
    scratch = f"seasight_wp14d_repo_{os.getpid()}"
    engine = None
    try:
        _create_scratch(admin, scratch)
        _apply_schema_sql(scratch, params)
        # 模拟存量环境：t_task_ack 尚不存在（由迁移 1700 创建）
        _execute_sql({**params, "dbname": scratch}, "DROP TABLE IF EXISTS t_task_ack")
        # 1600 存量环境不应预先拥有 1900 才引入的知识域表。
        for table in KNOWLEDGE_TABLES:
            _execute_sql(
                {**params, "dbname": scratch},
                f"DROP TABLE IF EXISTS {table}",
            )
        # 对话助手两表由 20260922_1000 创建，1600 存量环境同样不应预先存在。
        for table in CHAT_TABLES:
            _execute_sql(
                {**params, "dbname": scratch},
                f"DROP TABLE IF EXISTS {table}",
            )
        # 从 1600 线性升级到 head（应用 1700 与后续 head）
        _point_settings_at_db(scratch, params)
        from alembic import command

        cfg = _alembic_config()
        command.stamp(cfg, AGENT_STATE_REVISION)
        command.upgrade(cfg, "head")
        ver = _query_rows(
            {**params, "dbname": scratch}, "SELECT version_num FROM alembic_version"
        )
        assert ver == [(CHAT_SESSION_REVISION,)], (
            f"alembic_version 应为 {CHAT_SESSION_REVISION}，实际 {ver}"
        )
        # 列集合与冻结契约一致（真实库验证）
        cols = {
            r[0]
            for r in _query_rows(
                {**params, "dbname": scratch},
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='t_task_ack'",
            )
        }
        assert cols == TASK_ACK_COLUMNS, "真实库 t_task_ack 列与冻结契约不一致"
        # 索引
        idx = {
            r[0]
            for r in _query_rows(
                {**params, "dbname": scratch},
                "SELECT indexname FROM pg_indexes WHERE tablename='t_task_ack'",
            )
        }
        assert TASK_ACK_INDEXES <= idx, f"真实库缺少冻结索引：{TASK_ACK_INDEXES - idx}"

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
def _wipe_ack_table(ack_db):
    """每个依赖真实库的测试结束后清空 t_task_ack，保证测试互不污染。

    模块级 scratch 库被多个测试共享：只在依赖它的测试类上挂
    ``pytestmark = pytest.mark.usefixtures("_wipe_ack_table")``，
    纯内存契约测试不触数据库。
    """
    yield

    async def _do() -> None:
        async with ack_db() as session:
            await session.execute(text("DELETE FROM t_task_ack"))
            await session.commit()

    asyncio.run(_do())


def _seed_task(factory, task_id: str, *, township: str = "马鼻镇") -> None:
    async def _do() -> None:
        async with factory() as session:
            session.add(
                Task(
                    task_id=task_id,
                    target_location="SRID=4326;POINT(119.6 26.3)",
                    status="assigned",
                    robot_id="RBT-DEMO-01",
                    township=township,
                )
            )
            await session.commit()

    asyncio.run(_do())


def _arrive(
    factory,
    *,
    command_id: str,
    task_id: str,
    device_id: str = "RBT-DEMO-01",
    seq: int = 1,
    outcome: str = "new",
    accepted: bool = True,
    reason: str = "",
    mode: str = "",
    received_at: datetime | None = None,
    received_wall_at: datetime | None = None,
    raw_payload: dict | None = None,
) -> str:
    async def _do() -> str:
        async with factory() as session:
            result = await TaskAckRepository(session).record_arrival(
                command_id=command_id,
                task_id=task_id,
                device_id=device_id,
                seq=seq,
                outcome=outcome,
                accepted=accepted,
                reason=reason,
                mode=mode,
                received_at=received_at or datetime(2026, 9, 19, 7, 59, 30, tzinfo=timezone.utc),
                raw_payload=raw_payload or {"command_id": command_id},
                received_wall_at=received_wall_at,
            )
            await session.commit()
            return result

    return asyncio.run(_do())


# ============================================================
# 4. TaskAckRepository 行为（真实 scratch PostgreSQL）
# ============================================================
class TestRecordArrival:
    """首次到达建规范行；重复到达只累计、不覆盖首次。"""

    pytestmark = pytest.mark.usefixtures("_wipe_ack_table")

    def test_first_arrival_creates_canonical_row(self, ack_db) -> None:
        _seed_task(ack_db, "tsk_ack_001")
        wall = datetime(2026, 9, 19, 8, 0, 0, tzinfo=timezone.utc)
        received = datetime(2026, 9, 19, 7, 59, 30, tzinfo=timezone.utc)
        payload = {"ack_id": "ack_001", "command_id": "cmd_tsk_ack_001", "accepted": True}

        result = _arrive(
            ack_db,
            command_id="cmd_tsk_ack_001", task_id="tsk_ack_001", seq=1,
            outcome="new", accepted=True, reason="dispatched", mode="navigating",
            received_at=received, received_wall_at=wall, raw_payload=payload,
        )
        assert result == AckResult.NEW

        async def _check() -> None:
            async with ack_db() as session:
                row = await TaskAckRepository(session).get_by_command_id("cmd_tsk_ack_001")
                assert row is not None
                assert row.task_id == "tsk_ack_001"
                assert row.device_id == "RBT-DEMO-01"
                assert row.seq == 1
                assert row.outcome == "new"
                assert row.accepted is True
                assert row.reason == "dispatched"
                assert row.mode == "navigating"
                assert row.received_at == received          # 设备回执时间
                assert row.received_wall_at == wall          # 平台首次落库时间
                assert row.duplicate_count == 0
                assert row.last_duplicate_at is None
                assert row.raw_payload == payload            # 首次规范回执原文留档
                assert row.last_payload is None

        asyncio.run(_check())

    def test_duplicate_bumps_counter_without_overwriting(self, ack_db) -> None:
        """重复到达只累计：不得覆盖首次规范回执（outcome/received_at/raw_payload）。"""
        _seed_task(ack_db, "tsk_ack_002")
        first_payload = {"ack_id": "ack_first", "accepted": False, "reason": "busy"}
        dup_payload = {"ack_id": "ack_dup", "accepted": True}
        received1 = datetime(2026, 9, 19, 7, 59, 30, tzinfo=timezone.utc)
        wall1 = datetime(2026, 9, 19, 8, 0, 0, tzinfo=timezone.utc)
        wall2 = datetime(2026, 9, 19, 8, 5, 0, tzinfo=timezone.utc)

        assert (
            _arrive(
                ack_db,
                command_id="cmd_tsk_ack_002", task_id="tsk_ack_002", seq=1,
                outcome="new", accepted=False, reason="busy",
                received_at=received1, received_wall_at=wall1, raw_payload=first_payload,
            )
            == AckResult.NEW
        )
        # 重复回执：内容不同（甚至 accepted 翻转），也绝不能改写首次规范回执
        result = _arrive(
            ack_db,
            command_id="cmd_tsk_ack_002", task_id="tsk_ack_002", seq=1,
            outcome="late", accepted=True,
            received_at=datetime(2026, 9, 19, 8, 4, 0, tzinfo=timezone.utc),
            received_wall_at=wall2, raw_payload=dup_payload,
        )
        assert result == AckResult.DUPLICATE

        async def _check() -> None:
            async with ack_db() as session:
                rows = (
                    await session.execute(
                        select(TaskAck).where(TaskAck.command_id == "cmd_tsk_ack_002")
                    )
                ).scalars().all()
                assert len(rows) == 1, "重复到达不得产生第二条规范行"
                row = rows[0]
                assert row.duplicate_count == 1
                assert row.last_duplicate_at == wall2
                assert row.last_payload == dup_payload
                # 首次规范回执原封不动
                assert row.outcome == "new"
                assert row.accepted is False
                assert row.received_at == received1
                assert row.received_wall_at == wall1
                assert row.raw_payload == first_payload

        asyncio.run(_check())

    def test_duplicate_accumulates_multiple_times(self, ack_db) -> None:
        """多次重复：duplicate_count 逐次累计，规范行仍然只有一条。"""
        _seed_task(ack_db, "tsk_ack_003")
        assert (
            _arrive(ack_db, command_id="cmd_tsk_ack_003", task_id="tsk_ack_003", seq=2)
            == AckResult.NEW
        )
        for i in range(3):
            assert (
                _arrive(
                    ack_db,
                    command_id="cmd_tsk_ack_003", task_id="tsk_ack_003", seq=2,
                    raw_payload={"n": i},
                )
                == AckResult.DUPLICATE
            )

        async def _check() -> None:
            async with ack_db() as session:
                rows = (
                    await session.execute(
                        select(TaskAck).where(TaskAck.command_id == "cmd_tsk_ack_003")
                    )
                ).scalars().all()
                assert len(rows) == 1
                assert rows[0].duplicate_count == 3
                assert rows[0].last_payload == {"n": 2}     # 最近一次重复回执
                assert rows[0].raw_payload == {"command_id": "cmd_tsk_ack_003"}

        asyncio.run(_check())


class _EmptyResult:
    """首个 SELECT 未命中的模拟结果（竞争窗口）。"""

    def scalar_one_or_none(self):
        return None


class _RacingSession:
    """模拟并发首次到达竞争：

    - 第 1 次 ``execute``（SELECT 查重）假装未命中 —— 对方事务尚未提交；
    - 随后的 ``flush`` 走真实会话 —— 真实撞唯一键（``IntegrityError``）；
    - ``rollback`` 后的重查命中对方已提交的规范行 → 转 duplicate。
    """

    def __init__(self, real) -> None:
        self._real = real
        self._selects = 0

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def execute(self, stmt, *args, **kwargs):
        self._selects += 1
        if self._selects == 1:
            return _EmptyResult()
        return await self._real.execute(stmt, *args, **kwargs)


class TestConcurrentUniqueConflict:
    """并发唯一冲突转 duplicate，不产生第二条规范行。"""

    pytestmark = pytest.mark.usefixtures("_wipe_ack_table")

    def test_race_converts_to_duplicate_no_second_row(self, ack_db) -> None:
        """并发唯一冲突必须转为 duplicate，不得产生第二条规范行（WP-14D §2）。"""
        _seed_task(ack_db, "tsk_ack_race")
        first_payload = {"ack_id": "ack_race_first", "accepted": True}
        assert (
            _arrive(
                ack_db,
                command_id="cmd_tsk_ack_race", task_id="tsk_ack_race", seq=1,
                outcome="new", accepted=True, raw_payload=first_payload,
            )
            == AckResult.NEW
        )

        async def _race() -> str:
            async with ack_db() as session:
                racing = _RacingSession(session)
                result = await TaskAckRepository(racing).record_arrival(
                    command_id="cmd_tsk_ack_race",
                    task_id="tsk_ack_race",
                    device_id="RBT-DEMO-01",
                    seq=1,
                    outcome="new",          # 竞争双方都判 new，账本只能留一条
                    accepted=True,
                    reason="",
                    mode="",
                    received_at=datetime(2026, 9, 19, 7, 59, 30, tzinfo=timezone.utc),
                    raw_payload={"ack_id": "ack_race_dup", "accepted": True},
                )
                await session.commit()
                return result

        assert asyncio.run(_race()) == AckResult.DUPLICATE

        async def _check() -> None:
            async with ack_db() as session:
                rows = (
                    await session.execute(
                        select(TaskAck).where(TaskAck.command_id == "cmd_tsk_ack_race")
                    )
                ).scalars().all()
                assert len(rows) == 1, "并发冲突不得产生第二条规范行"
                assert rows[0].duplicate_count == 1
                assert rows[0].raw_payload == first_payload   # 首次规范回执未被覆盖

        asyncio.run(_check())


class TestRebuildAckTracker:
    """重启恢复：从 t_task_ack 重建规范回执与设备 ACK 水位。"""

    pytestmark = pytest.mark.usefixtures("_wipe_ack_table")

    def test_restart_restores_dedup_and_device_watermark(self, ack_db) -> None:
        """重启恢复：AckTracker 从 t_task_ack 重建规范回执与设备 ACK 水位。"""
        _seed_task(ack_db, "tsk_ack_004")
        # 同一设备三条命令：new(seq=5) / out_of_order(seq=3) / late(seq=7)
        for command_id, seq, outcome in (
            ("cmd_ack_a", 5, "new"),
            ("cmd_ack_b", 3, "out_of_order"),
            ("cmd_ack_c", 7, "late"),
        ):
            assert (
                _arrive(
                    ack_db,
                    command_id=command_id, task_id="tsk_ack_004", seq=seq,
                    outcome=outcome, accepted=True,
                    received_wall_at=datetime(2026, 9, 19, 8, 0, 0, tzinfo=timezone.utc),
                )
                == outcome
            )

        # 「进程重启」：全新 AckTracker，从账本重建
        tracker = AckTracker()

        async def _rebuild() -> int:
            async with ack_db() as session:
                return await TaskAckRepository(session).rebuild_ack_tracker(tracker)

        n = asyncio.run(_rebuild())
        assert n == 3

        # 规范回执恢复
        assert tracker.is_acked("cmd_ack_a")
        first = tracker.ack_for("cmd_ack_a")
        assert first is not None
        assert first.outcome == "new"
        assert first.accepted is True
        assert tracker.ack_for("cmd_ack_c").outcome == "late"  # type: ignore[union-attr]

        # duplicate 判定恢复：同 command_id 再达 → duplicate
        env = AckEnvelope(
            command_id="cmd_ack_a", task_id="tsk_ack_004", device_id="RBT-DEMO-01",
            seq=5, received_at=100.0, accepted=True, reason="", mode="",
        )
        assert tracker.classify(env) == AckResult.DUPLICATE

        # 设备水位恢复：低于已 ACK 最高水位（7，含 late 行）的新命令 → out_of_order
        env_low = AckEnvelope(
            command_id="cmd_ack_x", task_id="tsk_ack_004", device_id="RBT-DEMO-01",
            seq=6, received_at=100.0, accepted=True, reason="", mode="",
        )
        assert tracker.classify(env_low) == AckResult.OUT_OF_ORDER
        # 高于水位 → new
        env_high = AckEnvelope(
            command_id="cmd_ack_y", task_id="tsk_ack_004", device_id="RBT-DEMO-01",
            seq=8, received_at=100.0, accepted=True, reason="", mode="",
        )
        assert tracker.classify(env_high) == AckResult.NEW

    def test_rebuild_counts_only_ledger_rows(self, ack_db) -> None:
        """重复累计不参与重建（账本里只有规范行）；重建后重复仍按 duplicate 判。"""
        _seed_task(ack_db, "tsk_ack_005")
        assert (
            _arrive(ack_db, command_id="cmd_ack_dup1", task_id="tsk_ack_005", seq=1)
            == AckResult.NEW
        )
        assert (
            _arrive(ack_db, command_id="cmd_ack_dup1", task_id="tsk_ack_005", seq=1)
            == AckResult.DUPLICATE
        )

        tracker = AckTracker()

        async def _rebuild() -> int:
            async with ack_db() as session:
                return await TaskAckRepository(session).rebuild_ack_tracker(tracker)

        assert asyncio.run(_rebuild()) == 1     # 只重建规范行，重复不新增行
        assert tracker.is_acked("cmd_ack_dup1")
        env = AckEnvelope(
            command_id="cmd_ack_dup1", task_id="tsk_ack_005", device_id="RBT-DEMO-01",
            seq=1, received_at=100.0, accepted=True, reason="", mode="",
        )
        assert tracker.classify(env) == AckResult.DUPLICATE


class TestListByTask:
    """分页查询：默认按 received_wall_at DESC，只返回该任务的行。"""

    pytestmark = pytest.mark.usefixtures("_wipe_ack_table")

    def test_paginated_desc_by_received_wall_at(self, ack_db) -> None:
        """分页查询：默认按 received_wall_at DESC，只返回该任务的行。"""
        _seed_task(ack_db, "tsk_ack_006")
        _seed_task(ack_db, "tsk_ack_other")
        base = datetime(2026, 9, 19, 8, 0, 0, tzinfo=timezone.utc)
        for i, command_id in enumerate(("cmd_l1", "cmd_l2", "cmd_l3")):
            assert (
                _arrive(
                    ack_db,
                    command_id=command_id, task_id="tsk_ack_006", seq=i + 1,
                    received_wall_at=base.replace(minute=10 + i),
                )
                == AckResult.NEW
            )
        # 别的任务的行不混入
        assert (
            _arrive(
                ack_db, command_id="cmd_other", task_id="tsk_ack_other", seq=1,
                received_wall_at=base.replace(minute=30),
            )
            == AckResult.NEW
        )

        async def _page(page: int, page_size: int) -> tuple[list[str], int]:
            async with ack_db() as session:
                rows, total = await TaskAckRepository(session).list_by_task(
                    "tsk_ack_006", page=page, page_size=page_size
                )
                return [r.command_id for r in rows], total

        page1, total = asyncio.run(_page(1, 2))
        assert total == 3
        assert page1 == ["cmd_l3", "cmd_l2"]          # 倒序
        page2, total = asyncio.run(_page(2, 2))
        assert total == 3
        assert page2 == ["cmd_l1"]
        page3, total = asyncio.run(_page(3, 2))
        assert page3 == []
        assert total == 3


class TestHandlerIntegration:
    """handler 与真实数据库的端到端接线（WP-14D §3 同事务闭环）。

    不注入假仓储/假引擎：真实 AsyncSession + 真实 TaskRepository +
    真实 DispatchEngine + 真实 TaskAckRepository —— 验证「账本写入与状态
    推进同一事务、QoS1 重复不重复推进」在生产路径上成立。
    """

    pytestmark = pytest.mark.usefixtures("_wipe_ack_table")

    def test_ack_records_ledger_and_advances_same_transaction(self, ack_db) -> None:
        from app.mqtt.handlers import handle_robot_ack

        async def _seed() -> None:
            async with ack_db() as session:
                session.add(
                    Task(
                        task_id="tsk_hook_001",
                        target_location="SRID=4326;POINT(119.6 26.3)",
                        status="assigned",
                        robot_id="RBT-DEMO-01",
                        township="马鼻镇",
                    )
                )
                await session.commit()

        asyncio.run(_seed())
        payload = {
            "command_id": "cmd_tsk_hook_001",
            "device_id": "RBT-DEMO-01",
            "seq": 1,
            "received_at": 100.0,
            "accepted": True,
            "reason": "dispatched",
            "mode": "navigating",
        }
        topic = "robot/RBT-DEMO-01/cmd/ack"
        tracker = AckTracker()

        async def _call() -> str | None:
            # handler 期望 session_factory()() 双重调用（工厂 → maker → 会话）
            return await handle_robot_ack(
                topic, payload, tracker=tracker, session_factory=lambda: ack_db
            )

        # 首次：new + 状态推进 + 账本规范行，同一事务提交
        assert asyncio.run(_call()) == AckResult.NEW

        async def _check_first() -> None:
            async with ack_db() as session:
                from app.models.task import Task
                from sqlalchemy import select as _select

                task = (
                    await session.execute(
                        _select(Task).where(Task.task_id == "tsk_hook_001")
                    )
                ).scalar_one()
                assert task.status == "navigating"
                row = await TaskAckRepository(session).get_by_command_id("cmd_tsk_hook_001")
                assert row is not None
                assert row.outcome == "new"
                assert row.accepted is True
                assert row.duplicate_count == 0
                assert row.raw_payload == payload

        asyncio.run(_check_first())

        # QoS1 重复：duplicate + 只累计账本，不重复推进（transition 仍只有一次）
        assert asyncio.run(_call()) == AckResult.DUPLICATE

        async def _check_dup() -> None:
            async with ack_db() as session:
                from app.models.task import Task
                from sqlalchemy import select as _select

                task = (
                    await session.execute(
                        _select(Task).where(Task.task_id == "tsk_hook_001")
                    )
                ).scalar_one()
                assert task.status == "navigating", "重复回执不得重复推进"
                rows = (
                    await session.execute(
                        _select(TaskAck).where(TaskAck.command_id == "cmd_tsk_hook_001")
                    )
                ).scalars().all()
                assert len(rows) == 1, "重复回执不得产生第二条规范行"
                assert rows[0].duplicate_count == 1
                assert rows[0].raw_payload == payload, "首次规范回执不得被覆盖"

        asyncio.run(_check_dup())

    def test_rejected_ack_records_ledger_and_reverts_same_transaction(self, ack_db) -> None:
        """accepted=false：账本写拒绝原因 + assigned -> pending 同事务提交。"""
        from app.mqtt.handlers import handle_robot_ack

        async def _seed() -> None:
            async with ack_db() as session:
                session.add(
                    Task(
                        task_id="tsk_hook_002",
                        target_location="SRID=4326;POINT(119.6 26.3)",
                        status="assigned",
                        robot_id="RBT-DEMO-01",
                        township="马鼻镇",
                    )
                )
                await session.commit()

        asyncio.run(_seed())
        payload = {
            "command_id": "cmd_tsk_hook_002",
            "device_id": "RBT-DEMO-01",
            "seq": 1,
            "received_at": 100.0,
            "accepted": False,
            "reason": "busy",
        }
        tracker = AckTracker()

        async def _call() -> str | None:
            return await handle_robot_ack(
                "robot/RBT-DEMO-01/cmd/ack", payload,
                tracker=tracker, session_factory=lambda: ack_db,
            )

        assert asyncio.run(_call()) == AckResult.NEW

        async def _check() -> None:
            async with ack_db() as session:
                from app.models.task import Task
                from sqlalchemy import select as _select

                task = (
                    await session.execute(
                        _select(Task).where(Task.task_id == "tsk_hook_002")
                    )
                ).scalar_one()
                assert task.status == "pending"
                assert task.robot_id is None
                assert task.remark == "设备拒绝：busy"
                row = await TaskAckRepository(session).get_by_command_id("cmd_tsk_hook_002")
                assert row is not None
                assert row.accepted is False
                assert row.reason == "busy"

        asyncio.run(_check())


# ============================================================
# 5. 真实数据库迁移：01_schema.sql 幂等 → upgrade → downgrade 回环
# ============================================================
@pytest.mark.db
def test_real_migration_roundtrip_on_scratch_db() -> None:
    """真实迁移验证：scratch 库上 01_schema.sql ×2（幂等）→ 模拟存量库
    （drop t_task_ack）→ stamp 1600 → upgrade head → 校验列/索引/外键 +
    冒烟写读 → downgrade 1600 → 校验（t_task_ack 删除、业务表保留）。

    若本机无可用 PostgreSQL/PostGIS 则跳过并说明；静态契约对账已执行。
    """
    import psycopg2
    from alembic import command

    params = _db_params()
    if not _db_reachable(params):
        pytest.skip("本机无可用 PostgreSQL/PostGIS —— 跳过真实迁移验证；静态契约对账已执行")

    admin = _scratch_admin(params)
    scratch = f"seasight_wp14d_mig_{os.getpid()}"

    try:
        _create_scratch(admin, scratch)
        _apply_schema_sql(scratch, params)   # 两次 → 幂等

        # 幂等断言：表集合与业务表完好
        tables = _db_tables({**params, "dbname": scratch})
        assert "t_task_ack" in tables
        for t in EXISTING_BUSINESS_TABLES:
            assert t in tables

        # 模拟存量环境：t_task_ack 尚未由迁移创建
        _execute_sql({**params, "dbname": scratch}, "DROP TABLE IF EXISTS t_task_ack")
        for table in KNOWLEDGE_TABLES:
            _execute_sql(
                {**params, "dbname": scratch},
                f"DROP TABLE IF EXISTS {table}",
            )
        # 对话助手两表由 20260922_1000 创建，1600 存量环境同样不应预先存在。
        for table in CHAT_TABLES:
            _execute_sql(
                {**params, "dbname": scratch},
                f"DROP TABLE IF EXISTS {table}",
            )

        # 从 1600 线性升级到 head（应用 1700 与后续 head）
        _point_settings_at_db(scratch, params)
        cfg = _alembic_config()
        command.stamp(cfg, AGENT_STATE_REVISION)
        command.upgrade(cfg, "head")

        ver = _query_rows({**params, "dbname": scratch}, "SELECT version_num FROM alembic_version")
        assert ver == [(CHAT_SESSION_REVISION,)], (
            f"alembic_version 应为 {CHAT_SESSION_REVISION}，实际 {ver}"
        )

        # 列集合
        cols = {
            r[0]
            for r in _query_rows(
                {**params, "dbname": scratch},
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='t_task_ack'",
            )
        }
        assert cols == TASK_ACK_COLUMNS
        # 唯一约束：command_id
        uniq = {
            r[0]
            for r in _query_rows(
                {**params, "dbname": scratch},
                "SELECT conname FROM pg_constraint c "
                "WHERE c.conrelid = 't_task_ack'::regclass AND c.contype='u'",
            )
        }
        assert "uq_task_ack_command_id" in uniq
        # 外键：task_id → t_task.task_id ON DELETE CASCADE
        fk = _query_rows(
            {**params, "dbname": scratch},
            "SELECT conname, confdeltype FROM pg_constraint c "
            "WHERE c.conrelid = 't_task_ack'::regclass AND c.contype='f'",
        )
        assert ("fk_task_ack_task", "c") in fk, "外键必须 ON DELETE CASCADE"
        # 索引
        idx = {
            r[0]
            for r in _query_rows(
                {**params, "dbname": scratch},
                "SELECT indexname FROM pg_indexes WHERE tablename='t_task_ack'",
            )
        }
        assert TASK_ACK_INDEXES <= idx

        # 冒烟写读：JSONB 原文可落库读回
        w = psycopg2.connect(**{**params, "dbname": scratch}, connect_timeout=3)
        w.autocommit = True
        wc = w.cursor()
        wc.execute(
            "INSERT INTO t_task (task_id, target_location, status) "
            "VALUES ('tsk_smoke', ST_GeomFromText('POINT(119.6 26.3)', 4326), 'assigned')"
        )
        wc.execute(
            "INSERT INTO t_task_ack (command_id, task_id, device_id, seq, outcome, accepted, "
            "reason, mode, received_at, raw_payload) "
            "VALUES ('cmd_smoke', 'tsk_smoke', 'RBT-DEMO-01', 1, 'new', true, 'dispatched', "
            "'navigating', now(), '{\"ack_id\":\"ack_smoke\",\"accepted\":true}')"
        )
        wc.execute(
            "SELECT outcome, accepted, raw_payload->>'ack_id' FROM t_task_ack "
            "WHERE command_id='cmd_smoke'"
        )
        assert wc.fetchone() == ("new", True, "ack_smoke")
        # 外键 CASCADE 生效：删任务 → ACK 行级联删除
        wc.execute("DELETE FROM t_task WHERE task_id='tsk_smoke'")
        wc.execute("SELECT count(*) FROM t_task_ack WHERE command_id='cmd_smoke'")
        assert wc.fetchone()[0] == 0, "ON DELETE CASCADE 未生效"
        w.close()

        # ---- downgrade 到 1600（真实执行）：只删 t_task_ack，业务表保留 ----
        command.downgrade(cfg, AGENT_STATE_REVISION)
        remaining = _db_tables({**params, "dbname": scratch})
        assert "t_task_ack" not in remaining, "downgrade 后 t_task_ack 应被删除"
        for t in EXISTING_BUSINESS_TABLES:
            assert t in remaining, f"downgrade 误删既有业务表 {t}"
        ver = _query_rows({**params, "dbname": scratch}, "SELECT version_num FROM alembic_version")
        assert ver == [(AGENT_STATE_REVISION,)], ver
    finally:
        _restore_settings()
        _drop_scratch(admin, scratch)
