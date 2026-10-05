"""WP-02 test_agent_models.py — Agent 数据与迁移契约测试。

覆盖四层：
1. ORM 字段/约束与冻结契约对账（纯内存，无数据库依赖）。
2. 冻结枚举语义对账（手册 3.1 运行状态 / 3.2 步骤类型 / 3.3 错误码 / 3.5 风险级别）。
3. backend/db/init/01_schema.sql DDL 与契约对账（正则/AST 静态解析 + 幂等性标记）。
4. Alembic 迁移 upgrade/downgrade：
   - 静态一致性（AST：create_table 只建四张 agent 表、drop_table 只删四张 agent 表、
     revision 链正确、迁移可导入 ORM）；
   - 真实数据库迁移验证（若本机有可用 PostgreSQL/PostGIS 则创建独立 scratch 库
     执行 upgrade → 校验 → downgrade → 校验；无数据库则 pytest.skip 并说明）。
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import os
import re
from pathlib import Path

import pytest
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint

# 注册 t_agent_* 到 Base.metadata（同时验证 ORM 可导入、可被 Alembic 导入）
import app.models.agent  # noqa: F401,E402
import app.models.agent_state  # noqa: F401,E402
from app.db.session import Base  # noqa: E402
from app.models import (  # noqa: E402
    AgentApproval,
    AgentDecision,
    AgentErrorCode,
    AgentMemory,
    AgentMemoryType,
    AgentRiskLevel,
    AgentRun,
    AgentRunState,
    AgentRunStatus,
    AgentScopeType,
    AgentSourceType,
    AgentStep,
    AgentStepStatus,
    AgentStepType,
)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SQL_PATH = BACKEND_ROOT / "db" / "init" / "01_schema.sql"
ALEMBIC_VERSIONS_DIR = BACKEND_ROOT / "alembic" / "versions"
ALEMBIC_DIR = BACKEND_ROOT / "alembic"

BASELINE_REVISION = "20260918_1000_baseline"
AGENT_REVISION = "20260918_1100_agent_runtime"
# WP-07 集成（总控串行）：coverage_area 可空化迁移为当前 head。
# revision id 30 字符，须 <= alembic_version.version_num VARCHAR(32)。
COVERAGE_NULLABLE_REVISION = "20260919_1200_coverage_nullable"
# WP-09 数据修复：清理所有来源不可证明的存量 coverage_area。
COVERAGE_CLEAR_REVISION = "20260919_1300_clear_coverage"
# WP-10 集成：持久化续跑状态为当前迁移 head。
AGENT_STATE_REVISION = "20260919_1600_agent_state"
# WP-14D 集成：ACK 审计账本迁移接在 agent_state 之后。
TASK_ACK_REVISION = "20260919_1700_task_ack"
# 审批角色迁移接在 ACK 之后。
APPROVER_ROLE_REVISION = "20260919_1800_approver_role"
# 知识资产与本体迁移接在审批角色之后。
KNOWLEDGE_REVISION = "20260919_1900_knowledge_assets"
# 对话助手迁移接在知识域之后，是当前唯一 head。
CHAT_SESSION_REVISION = "20260922_1000_chat_session"

# 01_schema.sql 会为新库创建这些表；模拟 1600 存量库时须先移除。
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

AGENT_TABLES = ("t_agent_run", "t_agent_step", "t_agent_memory", "t_agent_approval")
AGENT_STATE_TABLE = "t_agent_run_state"
AGENT_STATE_COLUMNS = {
    "id", "run_id", "request_json", "runtime_state_json",
    "idempotency_key", "state_version", "created_at", "updated_at",
}

# ---------- 冻结契约（手册 3.7） ----------
CONTRACT_COLUMNS: dict[str, set[str]] = {
    "t_agent_run": {
        "id", "run_id", "trigger_type", "objective", "status", "policy_version",
        "started_at", "finished_at", "termination_reason", "trace_id",
        "created_at", "updated_at",
    },
    "t_agent_step": {
        "id", "step_id", "run_id", "step_no", "step_type", "decision_summary",
        "tool_name", "tool_version", "input_hash", "output_hash",
        "status", "latency_ms", "error_code", "created_at",
    },
    "t_agent_memory": {
        "id", "memory_id", "memory_type", "scope_type", "scope_id",
        "content", "confidence", "source_type", "source_id",
        "valid_from", "valid_to", "created_at",
    },
    "t_agent_approval": {
        "id", "approval_id", "run_id", "requested_action", "risk_level",
        "requested_by", "decided_by", "decision", "reason",
        "requested_at", "decided_at",
    },
}
CONTRACT_ALL_COLUMNS = {**CONTRACT_COLUMNS, AGENT_STATE_TABLE: AGENT_STATE_COLUMNS}

# 唯一约束契约：{表: {frozenset(列)}}
CONTRACT_UNIQUE: dict[str, set[frozenset[str]]] = {
    "t_agent_run": {frozenset({"run_id"})},
    "t_agent_step": {frozenset({"step_id"}), frozenset({"run_id", "step_no"})},
    "t_agent_memory": {frozenset({"memory_id"})},
    "t_agent_approval": {frozenset({"approval_id"})},
    AGENT_STATE_TABLE: {frozenset({"run_id"})},
}

# 外键契约：{表: (列, 目标表, 目标列)}
CONTRACT_FK: dict[str, tuple[str, str, str]] = {
    "t_agent_step": ("run_id", "t_agent_run", "run_id"),
    "t_agent_approval": ("run_id", "t_agent_run", "run_id"),
    AGENT_STATE_TABLE: ("run_id", "t_agent_run", "run_id"),
}

# ---------- 冻结枚举（手册 3.1 / 3.2 / 3.3 / 3.5） ----------
FROZEN_RUN_STATUSES = (
    "created", "planning", "waiting_policy", "waiting_approval",
    "executing", "observing", "verifying",
    "succeeded", "failed", "cancelled", "expired",
)
FROZEN_STEP_TYPES = (
    "plan", "policy", "approval_request", "tool_call",
    "observation", "verification", "replan", "terminal",
)
FROZEN_ERROR_CODES = (
    "no_robot_available", "tool_timeout", "tool_failed",
    "policy_denied", "approval_rejected", "approval_timeout",
    "task_conflict", "invalid_tool_input", "invalid_tool_output",
    "max_steps_exceeded", "run_expired", "internal_error",
)
FROZEN_RISK_LEVELS = ("read_only", "write", "device_command", "sensitive")

# 既有业务表（downgrade 不得删除）
EXISTING_BUSINESS_TABLES = (
    "t_device", "t_event", "t_task", "t_track", "t_report_daily", "t_user", "t_audit_log",
)


# ============================================================
# 帮助函数
# ============================================================

def _migration_file() -> Path:
    matches = sorted(ALEMBIC_VERSIONS_DIR.glob("*_agent_runtime.py"))
    assert len(matches) == 1, f"backend/alembic/versions/ 下应恰有一个 *_agent_runtime.py，实际: {matches}"
    return matches[0]


def _migration_ast() -> ast.Module:
    return ast.parse(_migration_file().read_text(encoding="utf-8"))


def _state_migration_file() -> Path:
    matches = sorted(ALEMBIC_VERSIONS_DIR.glob("*_agent_persistent_state.py"))
    assert len(matches) == 1, (
        "backend/alembic/versions/ 下应恰有一个 *_agent_persistent_state.py，"
        f"实际: {matches}"
    )
    return matches[0]


def _state_migration_ast() -> ast.Module:
    return ast.parse(_state_migration_file().read_text(encoding="utf-8"))


def _import_migration_module():
    spec = importlib.util.spec_from_file_location("wp02_agent_runtime", _migration_file())
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _agent_enum_names() -> list[str]:
    return sorted(_import_migration_module().AGENT_ENUMS.keys())


def _schema_sql() -> str:
    return SCHEMA_SQL_PATH.read_text(encoding="utf-8")


_TYPE_RE = re.compile(
    r"^\s*([a-z_][a-z0-9_]*)\s+"
    r"(BIGSERIAL|VARCHAR|TEXT|TIMESTAMPTZ|NUMERIC|INTEGER|INT|SMALLINT|agent_[a-z_]+_enum)\b",
    re.IGNORECASE,
)
_CONSTRAINT_RE = re.compile(
    r"^\s*CONSTRAINT\s+([a-z0-9_]+)\s+(UNIQUE|FOREIGN KEY|CHECK|PRIMARY KEY)\b",
    re.IGNORECASE,
)
_TABLE_BLOCK_RE = re.compile(
    r"CREATE TABLE IF NOT EXISTS (t_agent_[a-z_]+)\s*\((.*?)\);", re.S | re.IGNORECASE
)
_ENUM_BLOCK_RE = re.compile(r"CREATE TYPE ([a-z0-9_]+) AS ENUM \((.*?)\);", re.S | re.IGNORECASE)


def _parse_table_blocks(sql: str) -> dict[str, dict]:
    """静态解析 CREATE TABLE IF NOT EXISTS 块：返回 {表名: {columns, constraints}}。"""
    blocks: dict[str, dict] = {}
    for m in _TABLE_BLOCK_RE.finditer(sql):
        name = m.group(1)
        cols: set[str] = set()
        constraints: set[tuple[str, str]] = set()
        for line in m.group(2).splitlines():
            cm = _CONSTRAINT_RE.match(line)
            if cm:
                constraints.add((cm.group(1), cm.group(2).upper()))
                continue
            tm = _TYPE_RE.match(line)
            if tm:
                cols.add(tm.group(1))
        blocks[name] = {"columns": cols, "constraints": constraints}
    return blocks


def _parse_enum_blocks(sql: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in _ENUM_BLOCK_RE.finditer(sql):
        out[m.group(1)] = re.findall(r"'([^']*)'", m.group(2))
    return out


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
        conn = psycopg2.connect(host=params["host"], port=params["port"], dbname=params["dbname"],
                                user=params["user"], password=params["password"], connect_timeout=3)
        conn.close()
        return True
    except Exception:
        return False


_ENV_KEYS = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
_saved_env: dict[str, str | None] | None = None


def _point_settings_at_db(dbname: str, params: dict) -> None:
    """把 app.core.config.settings 指向指定库（Alembic env.py 会读它）。

    ★ 必须先记住原环境变量，_restore_settings 恢复，避免污染同会话中
      其它测试（它们可能启动读取 POSTGRES_* 的子进程）。
    """
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
    """恢复被 _point_settings_at_db 改动的环境变量并重建 settings。"""
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


def _alembic_config():
    from alembic.config import Config
    cfg = Config(str(ALEMBIC_DIR.parent / "alembic.ini"))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    return cfg


def _query_rows(params: dict, sql: str) -> list[tuple]:
    import psycopg2
    conn = psycopg2.connect(**params, connect_timeout=5)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return list(cur.fetchall())
    finally:
        conn.close()


def _db_tables(params: dict) -> set[str]:
    return {r[0] for r in _query_rows(
        params, "SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}


def _db_agent_columns(params: dict) -> dict[str, set[str]]:
    rows = _query_rows(
        params,
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name LIKE 't_agent%'",
    )
    out: dict[str, set[str]] = {}
    for tbl, col in rows:
        out.setdefault(tbl, set()).add(col)
    return out


def _db_unique_constraints(params: dict) -> dict[str, set[frozenset[str]]]:
    rows = _query_rows(
        params,
        "SELECT c.conrelid::regclass::text AS tbl, conname, "
        "       array_agg(a.attname ORDER BY u.ord) AS cols "
        "FROM pg_constraint c "
        "JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS u(attnum, ord) ON true "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = u.attnum "
        "WHERE c.contype = 'u' AND c.conrelid::regclass::text LIKE 't_agent%' "
        "GROUP BY tbl, conname ORDER BY tbl, conname",
    )
    out: dict[str, set[frozenset[str]]] = {}
    for tbl, _conname, cols in rows:
        out.setdefault(tbl, set()).add(frozenset(cols))
    return out


def _db_foreign_keys(params: dict) -> set[tuple[str, frozenset[str], str, frozenset[str]]]:
    """按位置配对 conkey/confkey，返回 {(本表, 本表列, 目标表, 目标列)}。"""
    rows = _query_rows(
        params,
        "SELECT c.conrelid::regclass::text AS tbl, "
        "       a.attname AS col, "
        "       c.confrelid::regclass::text AS ref_tbl, "
        "       a2.attname AS ref_col "
        "FROM pg_constraint c "
        "JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS u(attnum, ord) ON true "
        "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = u.attnum "
        "JOIN LATERAL unnest(c.confkey) WITH ORDINALITY AS u2(attnum, ord) ON u2.ord = u.ord "
        "JOIN pg_attribute a2 ON a2.attrelid = c.confrelid AND a2.attnum = u2.attnum "
        "WHERE c.contype = 'f' AND c.conrelid::regclass::text LIKE 't_agent%' "
        "ORDER BY tbl, u.ord",
    )
    by_fk: dict[tuple[str, str], tuple[set[str], set[str]]] = {}
    for tbl, col, ref_tbl, ref_col in rows:
        cols, ref_cols = by_fk.setdefault((tbl, ref_tbl), (set(), set()))
        cols.add(col)
        ref_cols.add(ref_col)
    return {
        (tbl, frozenset(cols), ref_tbl, frozenset(ref_cols))
        for (tbl, ref_tbl), (cols, ref_cols) in by_fk.items()
    }


# ============================================================
# 1. ORM 契约
# ============================================================

def test_orm_models_importable_via_package() -> None:
    """app.models 包导出 agent 模型（ORM 可被 Alembic/应用导入）。"""
    for cls in (AgentRun, AgentStep, AgentMemory, AgentApproval, AgentRunState):
        assert cls.__tablename__ in (*AGENT_TABLES, AGENT_STATE_TABLE)


def test_orm_agent_tables_registered() -> None:
    for name in (*AGENT_TABLES, AGENT_STATE_TABLE):
        assert name in Base.metadata.tables, f"Base.metadata 缺少 {name}"


def test_orm_columns_match_frozen_contract() -> None:
    for name, cols in CONTRACT_COLUMNS.items():
        assert set(Base.metadata.tables[name].columns.keys()) == cols, name


def test_orm_run_state_columns_match_contract() -> None:
    table = Base.metadata.tables[AGENT_STATE_TABLE]
    assert set(table.columns.keys()) == AGENT_STATE_COLUMNS


def test_orm_unique_constraints_match_contract() -> None:
    for table, expected in CONTRACT_UNIQUE.items():
        t = Base.metadata.tables[table]
        actual = {
            frozenset(c.columns.keys())
            for c in t.constraints
            if isinstance(c, UniqueConstraint)
        }
        assert actual == expected, table


def test_orm_fk_targets_run_id_not_primary_id() -> None:
    """外键必须关联 t_agent_run.run_id（冻结契约），而不是主键 id。"""
    for table, (col, target_table, target_col) in CONTRACT_FK.items():
        t = Base.metadata.tables[table]
        fks = [c for c in t.constraints if isinstance(c, ForeignKeyConstraint)]
        assert fks, f"{table} 缺少外键"
        fk_cols = {frozenset(c.columns.keys()) for c in fks}
        assert frozenset({col}) in fk_cols, f"{table} 缺外键列 {col}"
        targets = {c.elements[0].target_fullname for c in fks}
        assert f"{target_table}.{target_col}" in targets, f"{table} 外键目标应为 {target_table}.{target_col}"


# ============================================================
# 2. 冻结枚举语义（手册 3.1 / 3.2 / 3.3 / 3.5）
# ============================================================

def test_enum_run_status_matches_manual_31() -> None:
    assert AgentRunStatus.ALL == FROZEN_RUN_STATUSES
    assert tuple(AgentRunStatus.TERMINAL) == ("succeeded", "failed", "cancelled", "expired")
    assert len(AgentRunStatus.TERMINAL) == 4


def test_enum_step_type_matches_manual_32() -> None:
    assert AgentStepType.ALL == FROZEN_STEP_TYPES


def test_enum_error_code_matches_manual_33() -> None:
    assert AgentErrorCode.ALL == FROZEN_ERROR_CODES
    assert len(AgentErrorCode.ALL) == 12


def test_enum_risk_level_matches_manual_35() -> None:
    assert AgentRiskLevel.ALL == FROZEN_RISK_LEVELS


def test_enum_additional_design_values() -> None:
    """WP-02 设计值（手册未冻结；与 WP-01 内存模型对齐）。"""
    assert AgentStepStatus.ALL == ("pending", "ok", "failed", "succeeded", "cancelled", "expired")
    assert AgentDecision.ALL == ("approved", "rejected", "cancelled")
    assert AgentMemoryType.ALL == ("working", "episodic", "semantic", "policy", "eval")
    assert AgentScopeType.ALL == ("run", "step", "global")
    assert AgentSourceType.ALL == (
        "manual", "tool_call", "policy", "verification", "replan",
        "observation", "model", "human_review",
    )


def test_wp01_memory_model_compatibility() -> None:
    """与 WP-01 内核（app.services.agents）的枚举/值域直接对账。

    WP-01 文件未合入时跳过；合入后必须通过，保证 WP-01 的持久化
    适配器可以把内存对象原样写入本 ORM 对应的数据库表。
    """
    try:
        from app.services.agents.errors import ERROR_CODES
        from app.services.agents.memory import MEMORY_TYPES
        from app.services.agents.model import AgentStatus, AgentStepType, RiskLevel
        from app.services.agents.repositories import ApprovalRecord  # noqa: F401
    except ImportError as exc:
        pytest.skip(f"WP-01 内核尚未合入（{exc}），跳过运行时兼容对账")

    assert tuple(s.value for s in AgentStatus) == FROZEN_RUN_STATUSES
    assert tuple(s.value for s in AgentStepType) == FROZEN_STEP_TYPES
    assert tuple(s.value for s in RiskLevel) == FROZEN_RISK_LEVELS
    assert ERROR_CODES == set(AgentErrorCode.ALL)
    assert MEMORY_TYPES == AgentMemoryType.ALL
    # WP-01 审批决策值域 approved/rejected/cancelled 必须与数据库枚举完全一致
    wp01_decisions = {"approved", "rejected", "cancelled"}
    assert set(AgentDecision.ALL) == wp01_decisions


# ============================================================
# 3. 01_schema.sql DDL 契约与幂等性（静态解析）
# ============================================================

def test_schema_sql_has_agent_tables_with_contract_columns() -> None:
    sql = _schema_sql()
    blocks = _parse_table_blocks(sql)
    assert set(blocks.keys()) == set(CONTRACT_ALL_COLUMNS), (
        f"01_schema.sql 中的 agent 表: {sorted(blocks)}"
    )
    for name, cols in CONTRACT_COLUMNS.items():
        assert blocks[name]["columns"] == cols, f"{name} 字段与冻结契约不一致"


def test_schema_sql_unique_constraints_present() -> None:
    sql = _schema_sql()
    blocks = _parse_table_blocks(sql)
    for table, expected in CONTRACT_UNIQUE.items():
        uniq_constraints = {cname for cname, ctype in blocks[table]["constraints"] if ctype == "UNIQUE"}
        assert len(uniq_constraints) == len(expected), table
    # 关键唯一约束名逐项存在
    assert ("uq_agent_run_run_id", "UNIQUE") in blocks["t_agent_run"]["constraints"]
    assert ("uq_agent_step_step_id", "UNIQUE") in blocks["t_agent_step"]["constraints"]
    assert ("uq_agent_step_run_no", "UNIQUE") in blocks["t_agent_step"]["constraints"]
    assert ("uq_agent_memory_memory_id", "UNIQUE") in blocks["t_agent_memory"]["constraints"]
    assert ("uq_agent_approval_approval_id", "UNIQUE") in blocks["t_agent_approval"]["constraints"]


def test_schema_sql_fk_references_run_id() -> None:
    sql = _schema_sql()
    for table, (col, target_table, target_col) in CONTRACT_FK.items():
        block = re.search(
            rf"CREATE TABLE IF NOT EXISTS {table}\s*\((.*?)\);", sql, re.S | re.IGNORECASE
        ).group(1)  # type: ignore[union-attr]
        assert f"REFERENCES {target_table} ({target_col})" in block, f"{table} 外键缺失"
        assert f"FOREIGN KEY ({col})" in block, f"{table} 外键列缺失"


def test_schema_sql_enum_values_match_manual() -> None:
    enums = _parse_enum_blocks(_schema_sql())
    assert enums["agent_run_status_enum"] == list(FROZEN_RUN_STATUSES)
    assert enums["agent_step_type_enum"] == list(FROZEN_STEP_TYPES)
    assert enums["agent_error_code_enum"] == list(FROZEN_ERROR_CODES)
    assert enums["agent_risk_level_enum"] == list(FROZEN_RISK_LEVELS)


def test_schema_sql_idempotent_and_preserves_existing() -> None:
    """可重复执行：扩展/类型/表/索引全部幂等；既有表与 PostGIS 不受影响。"""
    sql = _schema_sql()
    # PostGIS 扩展保留
    assert "CREATE EXTENSION IF NOT EXISTS postgis" in sql
    # 每个 agent 表都是 IF NOT EXISTS
    for t in (*AGENT_TABLES, AGENT_STATE_TABLE):
        assert f"CREATE TABLE IF NOT EXISTS {t} " in sql
    # 每个 agent 枚举类型都有 duplicate_object 守卫
    for name in _agent_enum_names():
        m = re.search(rf"CREATE TYPE {name} AS ENUM", sql)
        assert m, f"缺少枚举 {name}"
        assert "duplicate_object" in sql[m.start():m.start() + 400]
    assert sql.count("EXCEPTION WHEN duplicate_object THEN NULL") >= 6
    # 索引幂等
    for idx in (
        "idx_agent_run_status", "idx_agent_run_created", "idx_agent_memory_scope",
        "idx_agent_approval_run", "idx_agent_approval_decision", "idx_agent_approval_requested",
        "idx_agent_run_state_version",
    ):
        assert re.search(rf"CREATE INDEX IF NOT EXISTS {idx}\s", sql), idx
    assert re.search(
        r"CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_run_state_idem_key\s", sql
    )
    # 既有业务表原样保留（仍是 IF NOT EXISTS）
    for t in EXISTING_BUSINESS_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {t} " in sql


# ============================================================
# 4. Alembic 迁移：静态一致性
# ============================================================

def test_migration_identity_and_chain() -> None:
    mod = _import_migration_module()
    assert mod.revision == AGENT_REVISION
    assert mod.down_revision == BASELINE_REVISION


def test_migration_imports_orm_for_autogenerate() -> None:
    src = _migration_file().read_text(encoding="utf-8")
    assert "import app.models.agent" in src, "迁移应导入 ORM，使 agent 表注册到 Base.metadata（Alembic 可导入）"


def test_state_migration_identity_and_import() -> None:
    namespace = {"__file__": str(_state_migration_file())}
    source = _state_migration_file().read_text(encoding="utf-8")
    exec(compile(source, str(_state_migration_file()), "exec"), namespace)
    assert namespace["revision"] == AGENT_STATE_REVISION
    assert namespace["down_revision"] == COVERAGE_CLEAR_REVISION
    assert "import app.models.agent_state" in source


def test_state_migration_upgrade_and_downgrade_only_touch_state_table() -> None:
    tree = _state_migration_ast()
    assert set(_migration_create_tables(tree)) == {AGENT_STATE_TABLE}
    assert _migration_drop_tables(tree) == {AGENT_STATE_TABLE}


def test_migration_upgrade_creates_only_agent_tables() -> None:
    tree = _migration_ast()
    created = _migration_create_tables(tree)
    assert set(created.keys()) == set(AGENT_TABLES), sorted(created)
    for name, cols in CONTRACT_COLUMNS.items():
        assert created[name] == cols, f"迁移 {name} 字段与冻结契约不一致"


def test_migration_downgrade_drops_only_agent_tables() -> None:
    tree = _migration_ast()
    dropped = _migration_drop_tables(tree)
    assert dropped == set(AGENT_TABLES), f"downgrade 只允许 drop 四张 agent 表: {dropped}"
    # 逐表确认不 drop 任何业务表
    assert not dropped.intersection(set(EXISTING_BUSINESS_TABLES))


def test_migration_enum_values_match_manual_and_sql() -> None:
    mod = _import_migration_module()
    sql_enums = _parse_enum_blocks(_schema_sql())
    for name, values in mod.AGENT_ENUMS.items():
        assert sql_enums[name] == list(values), f"迁移与 01_schema.sql 的枚举 {name} 不一致"
    assert mod.AGENT_ENUMS["agent_run_status_enum"] == FROZEN_RUN_STATUSES
    assert mod.AGENT_ENUMS["agent_step_type_enum"] == FROZEN_STEP_TYPES
    assert mod.AGENT_ENUMS["agent_error_code_enum"] == FROZEN_ERROR_CODES
    assert mod.AGENT_ENUMS["agent_risk_level_enum"] == FROZEN_RISK_LEVELS


def test_orm_sql_migration_triple_consistency() -> None:
    """ORM / 01_schema.sql / 迁移 三处字段集合两两一致，杜绝拷贝漂移。"""
    orm = {name: set(Base.metadata.tables[name].columns.keys()) for name in AGENT_TABLES}
    sql = {name: blk["columns"] for name, blk in _parse_table_blocks(_schema_sql()).items()}
    mig = _migration_create_tables(_migration_ast())
    for name in AGENT_TABLES:
        assert orm[name] == sql[name] == mig[name] == CONTRACT_COLUMNS[name], name


def test_run_state_orm_sql_migration_consistency() -> None:
    """WP-10 状态表在 ORM、初始 SQL 与增量迁移三处字段一致。"""
    orm = set(Base.metadata.tables[AGENT_STATE_TABLE].columns.keys())
    sql = _parse_table_blocks(_schema_sql())[AGENT_STATE_TABLE]["columns"]
    mig = _migration_create_tables(_state_migration_ast())[AGENT_STATE_TABLE]
    assert orm == sql == mig == AGENT_STATE_COLUMNS


def test_alembic_script_directory_chain() -> None:
    """ScriptDirectory 层面验证完整 revision 链。"""
    from alembic.script import ScriptDirectory
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == [CHAT_SESSION_REVISION]
    agent = script.get_revision(AGENT_REVISION)
    assert agent.down_revision == BASELINE_REVISION
    baseline = script.get_revision(BASELINE_REVISION)
    assert baseline.down_revision is None
    # WP-07 集成：coverage 可空化迁移挂在 agent_runtime 之后
    coverage = script.get_revision(COVERAGE_NULLABLE_REVISION)
    assert coverage.down_revision == AGENT_REVISION
    # WP-09 数据修复：只清理 not_available 的存量数值
    clear = script.get_revision(COVERAGE_CLEAR_REVISION)
    assert clear.down_revision == COVERAGE_NULLABLE_REVISION
    # WP-10 状态表：接在数据修复迁移之后，保持单一 head
    state = script.get_revision(AGENT_STATE_REVISION)
    assert state.down_revision == COVERAGE_CLEAR_REVISION
    # WP-14D ACK 账本：接在状态表之后，保持单一 head
    ack = script.get_revision(TASK_ACK_REVISION)
    assert ack.down_revision == AGENT_STATE_REVISION
    # 审批角色：接在 ACK 之后，保持单一 head
    approver = script.get_revision(APPROVER_ROLE_REVISION)
    assert approver.down_revision == TASK_ACK_REVISION
    # 知识域：接在审批角色之后，继续保持单一 head
    knowledge = script.get_revision(KNOWLEDGE_REVISION)
    assert knowledge.down_revision == APPROVER_ROLE_REVISION
    # 对话助手：接在知识域之后，成为新的单一 head
    chat = script.get_revision(CHAT_SESSION_REVISION)
    assert chat.down_revision == KNOWLEDGE_REVISION


def test_approver_role_migration_identity_and_ddl() -> None:
    """审批角色迁移必须线性接在 ACK 之后，并向 PostgreSQL 枚举补充 approver。"""
    path = ALEMBIC_VERSIONS_DIR / "20260919_1800_approver_role.py"
    namespace = {"__file__": str(path)}
    source = path.read_text(encoding="utf-8")
    exec(compile(source, str(path), "exec"), namespace)
    assert namespace["revision"] == APPROVER_ROLE_REVISION
    assert namespace["down_revision"] == TASK_ACK_REVISION
    assert "ADD VALUE IF NOT EXISTS 'approver' BEFORE 'viewer'" in source


# ============================================================
# 5. 真实数据库迁移验证（有 PostgreSQL/PostGIS 才执行）
# ============================================================

@pytest.mark.db
def test_real_migration_roundtrip_on_scratch_db() -> None:
    """真实迁移验证：scratch 库上执行 01_schema.sql（两次，幂等）→ 模拟存量库 →
    alembic upgrade head → 校验 → alembic downgrade → 校验（业务表保留、agent 表删除）。

    若本机无可用 PostgreSQL/PostGIS 或无法创建 scratch 库，则跳过并说明。
    """
    import psycopg2
    from alembic import command

    params = _db_params()
    if not _db_reachable(params):
        pytest.skip(
            "本机无可用 PostgreSQL/PostGIS —— 跳过真实迁移验证；"
            "静态契约对账（ORM/SQL/迁移 AST）已执行"
        )

    admin = {**params, "dbname": "postgres"}
    try:
        psycopg2.connect(**admin, connect_timeout=3).close()
    except Exception:
        admin = dict(params)  # 无 postgres 维护库时退回业务库

    scratch = f"seasight_wp02_test_{os.getpid()}"

    try:
        # ---- 准备 scratch 库 ----
        a = psycopg2.connect(**admin, connect_timeout=3)
        a.autocommit = True
        c = a.cursor()
        c.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{scratch}'")
        c.execute(f"DROP DATABASE IF EXISTS {scratch}")
        c.execute(f"CREATE DATABASE {scratch}")
        a.close()

        s = psycopg2.connect(**{**params, "dbname": scratch}, connect_timeout=3)
        s.autocommit = True
        cur = s.cursor()
        sql = _schema_sql()
        cur.execute(sql)   # 第一次
        cur.execute(sql)   # 第二次 → 幂等
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name LIKE 't_agent%'")
        assert {r[0] for r in cur.fetchall()} == set(CONTRACT_ALL_COLUMNS), (
            "01_schema.sql 后 agent 表集合不完整"
        )
        # 模拟存量库：删掉 agent 表、agent_state 表与 t_task_ack（业务表保留）
        # 注意按依赖逆序删除（state/approval/memory/step 先于 run），否则 FK 阻止 DROP
        for t in reversed((*AGENT_TABLES, AGENT_STATE_TABLE)):
            cur.execute(f"DROP TABLE IF EXISTS {t}")
        for e in _agent_enum_names():
            cur.execute(f"DROP TYPE IF EXISTS {e}")
        # WP-14D：t_task_ack 由增量迁移 1700 负责创建，存量环境同样缺失
        cur.execute("DROP TABLE IF EXISTS t_task_ack")
        # 知识域表由 1900 创建，1600 存量环境同样不应预先存在。
        for table in KNOWLEDGE_TABLES:
            cur.execute(f"DROP TABLE IF EXISTS {table}")
        # 对话助手两表由 20260922_1000 创建，1600 存量环境同样不应预先存在。
        for table in CHAT_TABLES:
            cur.execute(f"DROP TABLE IF EXISTS {table}")
        s.close()

        # ---- upgrade head（真实执行）----
        _point_settings_at_db(scratch, params)
        cfg = _alembic_config()
        command.upgrade(cfg, "head")

        ver = _query_rows({**params, "dbname": scratch},
                          "SELECT version_num FROM alembic_version")
        assert ver == [(CHAT_SESSION_REVISION,)], (
            f"alembic_version 应为 {CHAT_SESSION_REVISION}，实际 {ver}"
        )

        # 字段集合
        assert _db_agent_columns({**params, "dbname": scratch}) == CONTRACT_ALL_COLUMNS
        # 唯一约束（含 (run_id, step_no)）
        assert _db_unique_constraints({**params, "dbname": scratch}) == CONTRACT_UNIQUE
        # 外键指向 run_id
        fks = _db_foreign_keys({**params, "dbname": scratch})
        for table, (col, ref_tbl, ref_col) in CONTRACT_FK.items():
            assert (table, frozenset({col}), ref_tbl, frozenset({ref_col})) in fks, f"{table} 外键不符"

        # 写读冒烟：用 WP-01 内核真实词汇写入并读回（证明数据库接受运行时值域）
        w = psycopg2.connect(**{**params, "dbname": scratch}, connect_timeout=3)
        w.autocommit = True
        wc = w.cursor()
        wc.execute(
            "INSERT INTO t_agent_run (run_id, trigger_type, objective, status, policy_version) "
            "VALUES ('run_smoke', 'event', '冒烟目标', 'planning', 'rules-v1.0')"
        )
        wc.execute(
            "INSERT INTO t_agent_step (step_id, run_id, step_no, step_type, decision_summary, "
            "tool_name, status, error_code) "
            "VALUES ('stp_smoke', 'run_smoke', 1, 'tool_call', '摘要', 'event.get', 'ok', NULL)"
        )
        wc.execute(
            "INSERT INTO t_agent_memory (memory_id, memory_type, scope_type, scope_id, content, "
            "confidence, source_type, source_id) "
            "VALUES ('mem_smoke', 'working', 'robot', 'RBT-01', '摘要', 1.0, 'tool_call', 'run_smoke')"
        )
        wc.execute(
            "INSERT INTO t_agent_approval (approval_id, run_id, requested_action, risk_level, "
            "requested_by, decision) "
            "VALUES ('apr_smoke', 'run_smoke', '下发设备指令', 'device_command', 'planner', 'cancelled')"
        )
        wc.execute("SELECT status FROM t_agent_step WHERE step_id='stp_smoke'")
        assert wc.fetchone()[0] == "ok"
        wc.execute("SELECT scope_type, source_type FROM t_agent_memory WHERE memory_id='mem_smoke'")
        assert wc.fetchone() == ("robot", "tool_call")
        wc.execute("SELECT decision FROM t_agent_approval WHERE approval_id='apr_smoke'")
        assert wc.fetchone()[0] == "cancelled"
        w.close()

        # ---- downgrade 到 baseline（真实执行）----
        command.downgrade(cfg, BASELINE_REVISION)
        remaining = _db_tables({**params, "dbname": scratch})
        for t in (*AGENT_TABLES, AGENT_STATE_TABLE, "t_task_ack", *CHAT_TABLES):
            assert t not in remaining, f"downgrade 后 {t} 应被删除"
        for t in EXISTING_BUSINESS_TABLES:
            assert t in remaining, f"downgrade 误删既有业务表 {t}"
        ver = _query_rows({**params, "dbname": scratch},
                          "SELECT version_num FROM alembic_version")
        assert ver == [(BASELINE_REVISION,)], ver
    finally:
        _restore_settings()
        try:
            a = psycopg2.connect(**admin, connect_timeout=3)
            a.autocommit = True
            c = a.cursor()
            c.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{scratch}'")
            c.execute(f"DROP DATABASE IF EXISTS {scratch}")
            a.close()
        except Exception:
            pass
