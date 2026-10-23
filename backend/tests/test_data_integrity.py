"""WP-07 数据完整性契约测试（端到端「未统计 ≠ 真实 0」）。

契约点（对应总控纠偏指令与上位计划书第 8 周）：
1. **报表聚合真实可复现**：相同输入 → 相同输出。纯聚合函数确定性断言；
   并对 `aggregate_daily` 做源码守卫 —— 函数体内不允许出现随机/时钟/
   哈希等非确定性来源，行序必须确定（sorted）。
2. **分母为零语义**：比率类指标分母为 0 时输出 None（not_available），
   不以 0 冒充实测；「真实 0」（有记录但为 0）与「未统计」（无记录/null）结构区分。
3. **coverage_area 可空 + 可用性三态**：未统计 = NULL（不是 0）；聚合跳过
   null；available/partial/not_available 三态判定；API 层 null 透传不转 0；
   CSV 导出 null 留空不写 0。
4. **指标口径声明完整**：每个对外指标（报表 5 项 + 大屏 10 项 + 三个列表
   端点）都必须能回答 口径 / 分母 / 时间窗 / 数据来源，且非空。
5. **无来源硬编码数值（扫描断言）**：对外指标载荷不得出现裸数字字面量；
   seed 不得包含伪造面积；迁移文件含 ALTER 语句（静态验证）；
   01_schema.sql / ORM 模型可空化一致。

本文件不连数据库：全部为纯逻辑断言与 AST/文本源码守卫（conftest 约定）。
"""

from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path

import pytest

from app.api.v1.reports import coverage_area_out, coverage_csv_cell
from app.api.v1.stats import (
    CLASS_DISTRIBUTION_METRIC,
    DASHBOARD_METRICS,
    EVENT_TREND_METRIC,
    NOTIFICATIONS_METRIC,
    REQUIRED_METRIC_FIELDS as STATS_REQUIRED_FIELDS,
    safe_ratio,
)
from app.models.misc import ReportDaily
from app.services.report import (
    COVERAGE_AVAILABLE,
    COVERAGE_NOT_AVAILABLE,
    COVERAGE_PARTIAL,
    METRIC_DEFINITIONS,
    REQUIRED_METRIC_FIELDS as REPORT_REQUIRED_FIELDS,
    CoverageReport,
    aggregate_coverage,
    coverage_availability,
    coverage_for_rows,
    coverage_summary_availability,
)

REPORT_FILE = "backend/app/services/report.py"
STATS_FILE = "backend/app/api/v1/stats.py"
REPORTS_API_FILE = "backend/app/api/v1/reports.py"
MIGRATION_FILE = "backend/alembic/versions/20260919_1200_coverage_nullable.py"
REPAIR_MIGRATION_FILE = "backend/alembic/versions/20260919_1300_clear_coverage.py"
SEED_FILE = "backend/db/init/02_seed.sql"
SCHEMA_FILE = "backend/db/init/01_schema.sql"
MODEL_FILE = "backend/app/models/misc.py"


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


def _function(project_root: Path, rel_path: str, name: str) -> ast.AST:
    """返回指定函数节点；找不到直接抛错（守门失效必须显式暴露）。"""
    for node in ast.walk(_parse(project_root / rel_path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{rel_path} 中找不到函数 {name} —— 守门已失效")


def _code_body(src: str) -> str:
    """去掉模块 docstring，只留代码本体（文档说明允许出现目标字样）。"""
    return src.split('"""', 2)[2] if src.startswith('"""') else src


# ----------------------------------------------------------------------
# 1. 报表聚合真实可复现（相同输入 → 相同输出）
# ----------------------------------------------------------------------
class TestReproducibleAggregation:
    def test_aggregate_coverage_deterministic(self) -> None:
        """相同输入连续两次聚合 → 完全相同的输出（含类型一致）。"""
        inputs = [Decimal("12.345"), None, 0, "5.5", 2.125]
        first = aggregate_coverage(inputs)
        second = aggregate_coverage(list(inputs))
        assert first == second
        assert type(first) is type(second) is Decimal

    def test_aggregate_coverage_order_independent(self) -> None:
        """Decimal 逐项求和 + 最终一次取整 → 输入顺序不影响结果。"""
        amounts = [Decimal("1.234"), Decimal("2.346"), Decimal("0.12"), None]
        assert aggregate_coverage(amounts) == aggregate_coverage(list(reversed(amounts)))

    def test_availability_functions_deterministic(self) -> None:
        """可用性判定纯函数同样可复现。"""
        assert coverage_availability(2, 5) == coverage_availability(2, 5) == COVERAGE_PARTIAL
        assert (
            coverage_summary_availability(3, 5)
            == coverage_summary_availability(3, 5)
            == COVERAGE_PARTIAL
        )

    def test_aggregate_daily_has_no_nondeterminism_source(self, project_root: Path) -> None:
        """aggregate_daily 源码不得出现随机/时钟/哈希等非确定性来源。

        若聚合结果依赖 `now()`、随机数或 UUID，则相同数据库输入会得到
        不同输出，报表无法复现 —— 这正是 WP-07 要钉死的形状。
        注意：`datetime.combine(target_date, time.min)` 由入参推导，是确定的；
        只有 `*.now()` / `random.*` / `uuid.*` 这类调用才被禁止。
        """
        fn = _function(project_root, REPORT_FILE, "aggregate_daily")
        banned_paths: tuple[tuple[tuple[str, ...], str], ...] = (
            (("datetime", "now"), "时钟读取"),
            (("datetime", "utcnow"), "时钟读取"),
            (("random",), "随机数"),
            (("uuid",), "随机 ID"),
            (("secrets",), "加密随机"),
            (("time", "time"), "时钟读取"),
            (("time", "monotonic"), "时钟读取"),
            (("time", "perf_counter"), "时钟读取"),
            (("time", "process_time"), "时钟读取"),
            (("os", "urandom"), "加密随机"),
            (("hashlib",), "哈希（聚合内出现需人工复核确定性）"),
        )
        hits: list[str] = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            path = node.func
            parts: list[str] = []
            while isinstance(path, ast.Attribute):
                parts.append(path.attr)
                path = path.value
            if isinstance(path, ast.Name):
                parts.append(path.id)
            dotted = tuple(reversed(parts))
            for banned, reason in banned_paths:
                if len(banned) <= len(dotted) and dotted[: len(banned)] == banned:
                    hits.append(f"{'.'.join(dotted)}（{reason}）")
        assert not hits, (
            f"aggregate_daily 出现非确定性来源 {sorted(set(hits))} —— "
            "聚合必须对相同输入给出相同输出。"
        )

    def test_aggregate_daily_iterates_sorted(self, project_root: Path) -> None:
        """UPSERT 行序必须确定（sorted），否则相同数据不同批次写入顺序漂移。"""
        src = (project_root / REPORT_FILE).read_text(encoding="utf-8")
        fn_src = src[
            src.index("async def aggregate_daily") : src.index("async def daily_report_worker")
        ]
        assert "sorted(keys)" in fn_src, (
            "aggregate_daily 未用 sorted(keys) 确定行序 —— 聚合写入顺序不可复现。"
        )


# ----------------------------------------------------------------------
# 2. 分母为零语义
# ----------------------------------------------------------------------
class TestZeroDenominatorSemantics:
    def test_safe_ratio_zero_denominator_is_not_available(self) -> None:
        """分母为 0 → None（not_available），不以 0 冒充实测。"""
        assert safe_ratio(5, 0) is None
        assert safe_ratio(0, 0) is None

    def test_safe_ratio_true_zero_when_denominator_positive(self) -> None:
        """分母 > 0、分子 = 0 → 0.0（真实 0），与 not_available 结构不同。"""
        assert safe_ratio(0, 10) == 0.0

    def test_safe_ratio_normal_value(self) -> None:
        assert safe_ratio(1, 8) == 0.125

    def test_coverage_no_source_vs_true_zero(self) -> None:
        """报表域同样区分：无记录 → None；有记录但为 0 → Decimal('0.00')。"""
        assert aggregate_coverage(None) is None
        assert aggregate_coverage([]) is None
        zero = aggregate_coverage([0, 0])
        assert zero == Decimal("0.00")
        assert zero is not None

    def test_dashboard_rate_denominator_declared(self) -> None:
        """带分母的对外指标必须声明分母为零时的行为。"""
        rate = DASHBOARD_METRICS["robots_online_rate"]["分母"]
        assert "分母" in rate and "0" in rate, (
            "robots_online_rate 的分母声明未写明零分母行为 —— 契约不完整。"
        )


# ----------------------------------------------------------------------
# 3. coverage_area 可空 + 可用性三态（未统计 ≠ 真实 0）
# ----------------------------------------------------------------------
class TestCoverageNullAndAvailability:
    def test_aggregate_coverage_skips_null_records(self) -> None:
        """聚合跳过 null 记录：含 null 仍正确合计；全 null → None。"""
        assert aggregate_coverage([Decimal("12.345"), None, "5.5"]) == Decimal("17.85")
        assert aggregate_coverage([None, None]) is None

    def test_true_zero_distinct_from_null(self) -> None:
        """真实 0（有记录）与未统计（null）结构区分。"""
        assert aggregate_coverage([0, 0]) == Decimal("0.00")
        assert aggregate_coverage([0, 0]) is not None
        assert aggregate_coverage(None) is None

    def test_availability_three_states(self) -> None:
        """available / partial / not_available 三态判定。"""
        assert coverage_availability(0) == COVERAGE_NOT_AVAILABLE
        assert coverage_availability(5, 5) == COVERAGE_AVAILABLE
        assert coverage_availability(2, 5) == COVERAGE_PARTIAL

    def test_summary_availability_three_states(self) -> None:
        assert coverage_summary_availability(0, 5) == COVERAGE_NOT_AVAILABLE
        assert coverage_summary_availability(5, 5) == COVERAGE_AVAILABLE
        assert coverage_summary_availability(3, 5) == COVERAGE_PARTIAL

    def test_coverage_for_rows_null_passthrough(self) -> None:
        """行级入口：无记录 → (None, not_available)，绝不产出 0。"""
        assert coverage_for_rows(None, ("马鼻镇", "foam")) == (None, COVERAGE_NOT_AVAILABLE)
        m = {("黄岐镇", "foam"): CoverageReport(Decimal("12.50"), COVERAGE_AVAILABLE)}
        assert coverage_for_rows(m, ("马鼻镇", "foam")) == (None, COVERAGE_NOT_AVAILABLE)
        assert coverage_for_rows(m, ("黄岐镇", "foam")) == (Decimal("12.50"), COVERAGE_AVAILABLE)

    def test_reports_api_null_passthrough(self) -> None:
        """API 层 null 透传：None → None（不转 0）；Decimal → float。"""
        assert coverage_area_out(None) is None
        assert coverage_area_out(Decimal("12.50")) == 12.5
        assert coverage_area_out(Decimal("0.00")) == 0.0

    def test_csv_cell_null_not_zero(self) -> None:
        """CSV 导出：null → 留空（""），不写 0；真实 0 → "0.00"。"""
        assert coverage_csv_cell(None) == ""
        assert coverage_csv_cell(Decimal("12.5")) == "12.50"
        assert coverage_csv_cell(Decimal("0.00")) == "0.00"

    def test_report_daily_model_coverage_nullable(self) -> None:
        """ORM：coverage_area 可空（未统计=null），coverage_availability 列存在。"""
        cols = ReportDaily.__table__.c
        assert cols.coverage_area.nullable is True, (
            "t_report_daily.coverage_area 必须可空（NULL=未统计）。"
        )
        assert "coverage_availability" in cols, "缺少 coverage_availability 列"


# ----------------------------------------------------------------------
# 4. 指标口径声明完整（口径 / 分母 / 时间窗 / 来源）
# ----------------------------------------------------------------------
class TestMetricDefinitionsComplete:
    @pytest.mark.parametrize(
        "definitions,required",
        [
            (METRIC_DEFINITIONS, REPORT_REQUIRED_FIELDS),
            (DASHBOARD_METRICS, STATS_REQUIRED_FIELDS),
        ],
    )
    def test_definitions_all_have_required_fields(
        self, definitions: dict, required: tuple[str, ...]
    ) -> None:
        assert definitions, "指标口径表为空 —— 契约缺失"
        for name, meta in definitions.items():
            for field in required:
                assert field in meta, f"{name} 缺指标字段 {field}"
                assert str(meta[field]).strip(), f"{name} 的 {field} 为空"

    def test_dashboard_metrics_have_availability(self) -> None:
        """大屏每个指标都声明 availability（来源状态/零分母行为）。"""
        for name, meta in DASHBOARD_METRICS.items():
            assert meta.get("availability", "").strip(), f"{name} 缺 availability 声明"

    def test_dashboard_payload_keys_covered_by_definitions(self, project_root: Path) -> None:
        """大屏响应的每个指标键都有口径声明，且声明不悬空。

        指标载荷键与 DASHBOARD_METRICS 必须一一对应（除元数据键）。
        """
        fn = _function(project_root, STATS_FILE, "dashboard_stats")
        payload_keys: set[str] = set()
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "ok" or not node.args or not isinstance(node.args[0], ast.Dict):
                continue
            for key in node.args[0].keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    payload_keys.add(key.value)

        meta_keys = {"metrics_meta", "window_start", "window_end"}
        metric_payload = payload_keys - meta_keys
        assert metric_payload, "dashboard 指标载荷为空 —— 守门失效"
        assert metric_payload == set(DASHBOARD_METRICS), (
            "dashboard 指标键与 DASHBOARD_METRICS 不一致：\n"
            f"  载荷有、声明缺：{metric_payload - set(DASHBOARD_METRICS)}\n"
            f"  声明有、载荷缺：{set(DASHBOARD_METRICS) - metric_payload}\n"
            "每个对外指标必须既有声明又有真实来源。"
        )

    @pytest.mark.parametrize(
        "metric",
        [CLASS_DISTRIBUTION_METRIC, EVENT_TREND_METRIC, NOTIFICATIONS_METRIC],
        ids=["classes", "trend", "notifications"],
    )
    def test_list_endpoint_metrics_declared(self, metric: dict[str, str]) -> None:
        """列表类端点（保持载荷形状）以模块级声明文档化口径。"""
        for field in STATS_REQUIRED_FIELDS:
            assert field in metric, f"端点指标声明缺 {field}"
            assert str(metric[field]).strip(), f"端点指标声明的 {field} 为空"


# ----------------------------------------------------------------------
# 5. 无来源硬编码数值（扫描断言）
# ----------------------------------------------------------------------
class TestNoUnsourcedHardcodedValues:
    def test_dashboard_payload_has_no_literal_numbers(self, project_root: Path) -> None:
        """大屏指标载荷不得出现裸数字字面量。

        所有指标值必须来自仓库查询/派生表达式；手写常数意味着伪造或占位，
        WP-07 禁止。metrics_meta 等声明键是引用（Name），不在此列。
        """
        fn = _function(project_root, STATS_FILE, "dashboard_stats")
        offenders: list[str] = []
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "ok" or not node.args or not isinstance(node.args[0], ast.Dict):
                continue
            for key, value in zip(node.args[0].keys, node.args[0].values):
                if isinstance(value, ast.Constant) and isinstance(value.value, (int, float)):
                    offenders.append(f"{ast.unparse(key)} = {ast.unparse(value)}")
        assert not offenders, (
            f"dashboard 指标载荷出现无来源硬编码数字：{offenders} —— "
            "每个对外指标都必须来自真实查询，禁止手写数值冒充实测。"
        )

    def test_report_code_has_no_bare_coverage_zero_write(self, project_root: Path) -> None:
        """report.py 代码不得再出现裸 `coverage_area=0` 写入（未统计必须 null）。"""
        src = _code_body((project_root / REPORT_FILE).read_text(encoding="utf-8"))
        hits = re.findall(r"coverage_area\s*=\s*0\b", src)
        assert not hits, (
            f"report.py 代码仍存在裸写 {hits} —— coverage_area 假指标未清除；"
            "未统计必须写 null，经 coverage_for_rows。"
        )

    def test_reports_api_writes_null_not_zero(self, project_root: Path) -> None:
        """reports.py 必须显式处理 null 透传与 CSV 留空（不得 or 0 / float 报错）。"""
        src = (project_root / REPORTS_API_FILE).read_text(encoding="utf-8")
        assert "coverage_area_out" in src, "reports.py 未使用 coverage_area_out 空值透传"
        assert "coverage_csv_cell" in src, "reports.py 未使用 coverage_csv_cell 导出留空"
        assert "coverage_availability" in src, "reports.py 未输出 coverage_availability"

    def test_seed_has_no_fake_coverage_area(self, project_root: Path) -> None:
        """02_seed.sql 的报表种子不得包含编造面积：coverage_area 一律 NULL。

        每一行 VALUES 的第 8 列（coverage_area）必须为 NULL，
        第 9 列（coverage_availability）必须为 'not_available'。
        """
        src = (project_root / SEED_FILE).read_text(encoding="utf-8")
        start = src.index("INSERT INTO t_report_daily")
        end = src.index("ON CONFLICT", start)
        block = src[start:end]
        tuples = re.findall(r"\((CURRENT_DATE[^)]*)\)", block)
        assert tuples, "02_seed.sql 找不到 t_report_daily 的 VALUES 元组 —— 守门失效"
        for tup in tuples:
            fields = [f.strip() for f in tup.split(",")]
            assert len(fields) == 9, f"种子元组字段数异常：{fields}"
            assert fields[7] == "NULL", (
                f"种子 coverage_area 位置出现 {fields[7]!r} —— 禁止编造清扫面积，"
                "应写 NULL（未统计）。"
            )
            assert fields[8] == "'not_available'", (
                f"种子 coverage_availability 应为 'not_available'，实际 {fields[8]!r}"
            )

    def test_migration_contains_alter_statements(self, project_root: Path) -> None:
        """迁移文件必须含 ALTER 语句（静态验证）：可空化 + availability 列 + 回退。"""
        tree = _parse(project_root / MIGRATION_FILE)
        src = (project_root / MIGRATION_FILE).read_text(encoding="utf-8")
        assert "20260919_1200_coverage_nullable" in src, "迁移 revision 标识缺失"
        assert 'down_revision: str | None = "20260918_1100_agent_runtime"' in src, (
            "迁移 down_revision 必须链到 20260918_1100_agent_runtime"
        )
        upgrade_ops = [
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        ]
        assert "alter_column" in upgrade_ops, "迁移缺少 op.alter_column（coverage_area 可空化）"
        assert "drop_column" in upgrade_ops, "迁移 downgrade 缺少 op.drop_column"
        assert "ADD COLUMN IF NOT EXISTS coverage_availability" in src, (
            "迁移未以 ADD COLUMN IF NOT EXISTS 幂等加 availability 列（双轨安全）。"
        )
        assert "t_report_daily" in src and "coverage_area" in src and "coverage_availability" in src

    def test_repair_migration_clears_only_unattributed_values(self, project_root: Path) -> None:
        """存量修复必须只清 not_available，绝不能误删已证实的实测面积。"""
        tree = _parse(project_root / REPAIR_MIGRATION_FILE)
        src = (project_root / REPAIR_MIGRATION_FILE).read_text(encoding="utf-8")
        assert "20260919_1300_clear_coverage" in src
        assert 'down_revision: str | None = "20260919_1200_coverage_nullable"' in src
        sql_parts = [
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ]
        sql = "\n".join(sql_parts)
        assert "UPDATE t_report_daily" in sql
        assert "SET coverage_area = NULL" in sql
        assert "coverage_availability = 'not_available'" in sql
        assert "coverage_area IS NOT NULL" in sql
        assert "available" not in sql.replace("not_available", "")
        assert "partial" not in sql

    def test_schema_sql_nullable_ddl(self, project_root: Path) -> None:
        """01_schema.sql 的 t_report_daily DDL 必须可空 + availability 列，无 NOT NULL DEFAULT 0。"""
        src = (project_root / SCHEMA_FILE).read_text(encoding="utf-8")
        assert "coverage_area  NUMERIC(12,2)," in src, (
            "01_schema.sql 的 coverage_area 未改为可空（去掉 NOT NULL DEFAULT 0）。"
        )
        assert "coverage_availability VARCHAR(16) NOT NULL DEFAULT 'not_available'" in src, (
            "01_schema.sql 缺少 coverage_availability 列。"
        )
        assert "coverage_area  NUMERIC(12,2) NOT NULL DEFAULT 0" not in src, (
            "01_schema.sql 仍是旧的 NOT NULL DEFAULT 0 定义。"
        )

    def test_stats_does_not_output_coverage(self, project_root: Path) -> None:
        """stats 域代码不得输出 coverage_area —— 它没有可靠来源，出现即伪造风险。"""
        src = _code_body((project_root / STATS_FILE).read_text(encoding="utf-8"))
        assert "coverage_area" not in src, (
            "stats.py 代码出现了 coverage_area —— 该指标无可靠数据源，"
            "禁止在统计接口输出；报表域口径见 report.py。"
        )

    def test_model_matches_nullable_contract(self, project_root: Path) -> None:
        """ORM 模型必须与迁移/DDL 一致：coverage_area 声明可空 + availability 列。"""
        src = (project_root / MODEL_FILE).read_text(encoding="utf-8")
        assert "coverage_area: Mapped[Decimal | None]" in src, (
            "models/misc.py 的 coverage_area 未声明可空（Decimal | None）。"
        )
        assert "coverage_availability" in src, "models/misc.py 缺少 coverage_availability 列"

    def test_guards_are_not_noops(self, project_root: Path) -> None:
        """★ 自证：扫描器真的走进了源码，而不是空转通过。"""
        stats_src = (project_root / STATS_FILE).read_text(encoding="utf-8")
        report_src = (project_root / REPORT_FILE).read_text(encoding="utf-8")
        seed_src = (project_root / SEED_FILE).read_text(encoding="utf-8")
        migration_src = (project_root / MIGRATION_FILE).read_text(encoding="utf-8")
        assert "def dashboard_stats" in stats_src
        assert "async def aggregate_daily" in report_src
        assert "coverage_for_rows" in report_src and "coverage_availability" in report_src
        assert "INSERT INTO t_report_daily" in seed_src
        assert "def upgrade" in migration_src and "def downgrade" in migration_src
