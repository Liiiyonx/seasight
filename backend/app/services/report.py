"""报表定时聚合 —— 让 t_report_daily 真正由明细表生产。

背景（产品完整性缺陷）
----------------------
`t_report_daily` 曾经只由 `db/init/02_seed.sql` 灌入，代码里**没有任何
定时聚合任务** —— 系统跑一年，报表页的数字也一动不动。
更糟的是 `main.py` 的 docstring 早早就写了「启动定时任务（补派、报表聚合）」，
但实现只启动了补派，报表聚合从未接线：这是「声明 N 项、实现 N-1 项」
缺陷形状的又一例，而且因为是 docstring 与实现的错位，grep 都抓不到。

本模块补上缺失的最后一环：每天把明细表（t_event / t_task）聚合进宽表。

乡镇归属的单一真源
------------------
事件与任务都没有 `township` 字段，只有 POINT 坐标。乡镇归属按
「距离最近乡镇中心点」判定，中心点坐标在此处集中定义（`TOWNSHIPS`），
与 `db/init/02_seed.sql` 注释里的坐标、`frontend/src/utils/constants.js`
的 `TOWNSHIPS` 列表保持对账（有测试钉住，防止第五处定义漂移）。

coverage_area 的诚实语义（WP-07 数据完整性）
---------------------------------------------
历史缺陷：聚合无条件写 `coverage_area=0`，与 seed 里 15000/12000 之类
「拍脑袋数字」并存，语义不明（「清扫面积」还是「到达范围」？扫描宽度多少？）。
两者都不可信：0 冒充了实测，15000 冒充了治理成效。

WP-07 修复后的语义（端到端「未统计 ≠ 真实 0」）：
- 覆盖面积**没有可靠真实数据源**（机器人清扫面积上报链路未实现；t_track
  轨迹点稀疏，凹包面积不等于清扫覆盖面积）。
- 数据库列 `t_report_daily.coverage_area` **可空**（迁移
  `20260919_1200_coverage_nullable`）：未统计写 **NULL**，不是 0。
- 可用性三态（`coverage_availability` 列，判定见 `coverage_availability`）：
  * `not_available` = 无记录 / 未统计（coverage_area 为 NULL）；
  * `available`     = 有真实记录且覆盖完整；
  * `partial`       = 部分记录（上报单元数 < 期望单元数）。
- 当前生产环境没有覆盖面积数据源（`aggregate_daily` 的 `coverage_source`
  缺省为 None），聚合只产出 NULL + not_available；未来机器人上报链路
  落地后注入 `coverage_source` 即可产出 available/partial 与真实面积，
  聚合与可用性判定逻辑无需改动。
- 对外口径由 `METRIC_DEFINITIONS` 单一声明，报表 API 与测试都从这里读。

event_count / task_count / done_count / collected_kg 有可靠数据源，真实聚合。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, NamedTuple

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.geo import TOWNSHIPS, nearest_township
from app.db.session import get_session_factory
from app.models.event import Event
from app.models.misc import ReportDaily
from app.models.task import Task, TaskStatus

# 连江沿海乡镇中心点 —— 已抽到 app.core.geo（供事件/任务/报表三处共用，
# 避免循环 import）。这里 re-export 保持向后兼容。
# 每日聚合运行的时刻（本地时区），聚合「前一天」的数据
DAILY_RUN_HOUR = 1
DAILY_RUN_MINUTE = 0

# ----------------------------------------------------------------------
# coverage_area 可用性契约（WP-07 单一真源）
# ----------------------------------------------------------------------
# 三态定义（对外口径）：
#   available      = 有真实记录且覆盖完整
#   partial        = 部分记录（上报单元数 < 期望单元数）
#   not_available  = 无记录 / 未统计（coverage_area 写 NULL，不是 0）
COVERAGE_AVAILABLE = "available"
COVERAGE_PARTIAL = "partial"
COVERAGE_NOT_AVAILABLE = "not_available"


class CoverageReport(NamedTuple):
    """某 (township, main_class) 当日覆盖面积聚合结果（来自覆盖面积数据源）。

    - ``area_m2``   ：真实合计（可为 0.00 = 真实 0；未统计时为 None）。
    - ``availability``：available / partial / not_available 三态之一。
    """

    area_m2: Decimal | None
    availability: str


# 覆盖面积数据源接口：async (session, target_date) ->
#   dict[(township, main_class), CoverageReport]
# 当前生产环境没有数据源（aggregate_daily 的 coverage_source 缺省 None）
# → 所有行 coverage_area=NULL、coverage_availability='not_available'。
# 未来机器人清扫面积上报链路落地后注入真实 provider 即可，无需改聚合逻辑。
CoverageSource = Callable[
    [AsyncSession, date],
    Awaitable[dict[tuple[str, str], CoverageReport]],
]


def aggregate_coverage(
    records: Iterable[Decimal | int | float | None] | None,
) -> Decimal | None:
    """把上报的清扫面积聚合为一行 coverage_area 值；**跳过 None（未上报）**。

    返回 None 表示「未统计」（没有任何有效记录）—— 与「真实 0」结构区分：

    - ``None``            = 未统计（not_available），禁止以 0 冒充实测；
    - ``Decimal('0.00')`` = 真实 0（有记录但合计为 0）；
    - ``Decimal('x.xx')`` = 真实聚合值（两位小数，ROUND_HALF_UP）。

    纯函数：相同输入 → 相同输出，可直接做确定性回归。未来接入真实来源后，
    按 (township, main_class) 把各自上报面积传入即可复用，聚合语义不变。
    """
    if not records:
        return None
    values = [Decimal(str(r)) for r in records if r is not None]
    if not values:
        return None
    total = sum(values, Decimal("0"))
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def coverage_availability(reported_units: int, expected_units: int | None = None) -> str:
    """按「已上报单元数 / 期望单元数」判定覆盖面积可用性。

    - ``reported_units <= 0`` → not_available（无记录 / 未统计）；
    - ``expected_units`` 未提供，或 ``reported_units >= expected_units`` → available；
    - ``0 < reported_units < expected_units`` → partial（部分记录）。

    纯函数：相同输入 → 相同输出。
    """
    if reported_units <= 0:
        return COVERAGE_NOT_AVAILABLE
    if expected_units is None or reported_units >= expected_units:
        return COVERAGE_AVAILABLE
    return COVERAGE_PARTIAL


def coverage_summary_availability(measured_rows: int, total_rows: int) -> str:
    """汇总层（按乡镇聚合多行）的可用性判定。

    - ``total_rows <= 0`` 或 ``measured_rows <= 0`` → not_available；
    - ``measured_rows >= total_rows`` → available；
    - ``0 < measured_rows < total_rows`` → partial（部分行有实测）。

    纯函数：相同输入 → 相同输出。
    """
    if total_rows <= 0 or measured_rows <= 0:
        return COVERAGE_NOT_AVAILABLE
    if measured_rows >= total_rows:
        return COVERAGE_AVAILABLE
    return COVERAGE_PARTIAL


def coverage_for_rows(
    coverage_map: dict[tuple[str, str], CoverageReport] | None,
    key: tuple[str, str],
) -> tuple[Decimal | None, str]:
    """取某 (township, main_class) 行的覆盖信息；无记录 → (None, not_available)。

    聚合 UPSERT 的行级单一入口：禁止在别处直接拼写 coverage_area 数值。
    """
    if not coverage_map:
        return None, COVERAGE_NOT_AVAILABLE
    report = coverage_map.get(key)
    if report is None:
        return None, COVERAGE_NOT_AVAILABLE
    return report.area_m2, report.availability


# ----------------------------------------------------------------------
# 对外指标口径表（WP-07）：每个指标必须可回答
#   口径 / 分母 / 时间窗 / 数据来源
# ----------------------------------------------------------------------
METRIC_DEFINITIONS: dict[str, dict[str, str]] = {
    "event_count": {
        "label": "事件数",
        "口径": "当日 t_event 明细按 (乡镇, 垃圾类别) 归属后的计数；乡镇由事件坐标经 nearest_township 判定",
        "分母": "1（计数，不涉比率）",
        "时间窗": "stat_date 当日 00:00:00 ~ 次日 00:00:00（本地时区，按 Event.event_time）",
        "来源": "t_event（事件明细表）",
        "availability": "measured",
    },
    "task_count": {
        "label": "工单数",
        "口径": "当日创建且关联事件（event_id 非空）的 t_task 按 (乡镇, 类别) 计数；人工建单（event_id 为空）无 main_class，不进分类报表",
        "分母": "1（计数，不涉比率）",
        "时间窗": "stat_date 当日（按 Task.created_at）",
        "来源": "t_task（工单明细表，外连 t_event 取类别）",
        "availability": "measured",
    },
    "done_count": {
        "label": "完成数",
        "口径": "当日创建工单中状态为 done 的计数",
        "分母": "1（单独输出时分母为 1）；若用于完成率，分母为 task_count",
        "时间窗": "stat_date 当日（按 Task.created_at）",
        "来源": "t_task.status == done",
        "availability": "measured",
    },
    "collected_kg": {
        "label": "清理量",
        "口径": "当日完成工单（done）的 collected_weight 合计",
        "分母": "1（计量合计，不涉比率）",
        "时间窗": "stat_date 当日（按 Task.created_at）",
        "来源": "t_task.collected_weight（仅 done 工单）",
        "availability": "measured",
    },
    "coverage_area": {
        "label": "清扫覆盖面积",
        "口径": "机器人上报的真实清扫面积合计；当前无上报链路，不计算、不估算",
        "分母": "1（计量合计，不涉比率）",
        "时间窗": "stat_date 当日",
        "来源": "无（机器人清扫面积上报链路未实现；t_track 轨迹凹包不等于清扫覆盖面积）",
        "availability": COVERAGE_NOT_AVAILABLE,
        "备注": "未统计时列值为 NULL（不是 0），禁止把 NULL 当作 0 消费；真实 0 仅在 availability 为 available/partial 且合计为 0 时出现",
    },
}

REQUIRED_METRIC_FIELDS: tuple[str, ...] = ("口径", "分母", "时间窗", "来源")


async def aggregate_daily(
    session: AsyncSession,
    target_date: date,
    *,
    coverage_source: CoverageSource | None = None,
) -> int:
    """把 target_date 一天的明细聚合进 t_report_daily，返回写入行数。

    幂等：同一 (stat_date, township, main_class) 重复聚合会 UPSERT 覆盖，
    不会产生重复行。

    ``coverage_source``：可选覆盖面积数据源。缺省 None（当前生产状态，
    机器人清扫面积上报链路未实现）→ 每行 coverage_area=NULL、
    coverage_availability='not_available'，未统计不写 0。
    """
    day_start = datetime.combine(target_date, time.min)
    day_end = day_start + timedelta(days=1)

    # ---------- 0) 覆盖面积数据源（可选） ----------
    coverage_map: dict[tuple[str, str], CoverageReport] = {}
    if coverage_source is not None:
        coverage_map = await coverage_source(session, target_date)

    # ---------- 1) 事件数：按 (township, main_class) ----------
    ev_rows = await session.execute(
        select(
            Event.main_class,
            func.ST_X(Event.location).label("lng"),
            func.ST_Y(Event.location).label("lat"),
        ).where(Event.event_time >= day_start, Event.event_time < day_end)
    )
    event_counts: dict[tuple[str, str], int] = {}
    for main_class, lng, lat in ev_rows.all():
        if lng is None or lat is None:
            continue
        key = (nearest_township(float(lng), float(lat)), main_class)
        event_counts[key] = event_counts.get(key, 0) + 1

    # ---------- 2) 工单数 / 完成数 / 清理量：按 (township, main_class) ----------
    # 乡镇按 target_location（任务实际执行点）判定，比按事件点更贴近真实。
    # 人工建单（event_id 为 NULL）拿不到 main_class，这类任务不进分类报表，
    # 否则 (date, township, NULL) 会因 PostgreSQL 对 NULL 的等值语义而重复插行。
    tk_rows = await session.execute(
        select(
            Task.status,
            Task.collected_weight,
            func.ST_X(Task.target_location).label("lng"),
            func.ST_Y(Task.target_location).label("lat"),
            Event.main_class,
        )
        .outerjoin(Event, Event.event_id == Task.event_id)
        .where(Task.created_at >= day_start, Task.created_at < day_end)
    )
    task_agg: dict[tuple[str, str], dict[str, float]] = {}
    for status, weight, lng, lat, main_class in tk_rows.all():
        if lng is None or lat is None or main_class is None:
            continue
        key = (nearest_township(float(lng), float(lat)), main_class)
        row = task_agg.setdefault(key, {"task": 0, "done": 0, "kg": 0.0})
        row["task"] += 1
        if status == TaskStatus.DONE:
            row["done"] += 1
            row["kg"] += float(weight or 0)

    # ---------- 3) 合并 + UPSERT ----------
    keys = set(event_counts) | set(task_agg)
    # coverage_area / coverage_availability 走 coverage_for_rows 单一入口：
    # 无数据源 → (NULL, not_available)，未统计不写 0；有数据源 → 真实聚合值。
    for township, main_class in sorted(keys):
        ev = event_counts.get((township, main_class), 0)
        tk = task_agg.get((township, main_class), {"task": 0, "done": 0, "kg": 0.0})
        coverage_area_value, coverage_availability_value = coverage_for_rows(
            coverage_map, (township, main_class)
        )
        stmt = pg_insert(ReportDaily).values(
            stat_date=target_date,
            township=township,
            main_class=main_class,
            event_count=ev,
            task_count=int(tk["task"]),
            done_count=int(tk["done"]),
            collected_kg=tk["kg"],
            coverage_area=coverage_area_value,
            coverage_availability=coverage_availability_value,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_report_daily",
            set_={
                "event_count": stmt.excluded.event_count,
                "task_count": stmt.excluded.task_count,
                "done_count": stmt.excluded.done_count,
                "collected_kg": stmt.excluded.collected_kg,
                "coverage_area": stmt.excluded.coverage_area,
                "coverage_availability": stmt.excluded.coverage_availability,
            },
        )
        await session.execute(stmt)

    return len(keys)


async def daily_report_worker() -> None:
    """每日定时聚合：凌晨 DAILY_RUN_HOUR 点聚合「前一天」的数据。

    启动时先补一次「今天」的数据（让演示/刚部署时报表立即可见），
    之后按天滚动。任何一次失败只记日志，不影响下一轮。
    """
    from app.db.session import get_session_factory as _factory

    logger.info("[报表] 报表聚合任务已启动")

    while True:
        try:
            now = datetime.now()
            next_run = (now + timedelta(days=1)).replace(
                hour=DAILY_RUN_HOUR, minute=DAILY_RUN_MINUTE, second=0, microsecond=0
            )
            await asyncio.sleep(max(1.0, (next_run - now).total_seconds()))

            target = date.today() - timedelta(days=1)
            async with _factory()() as session:
                n = await aggregate_daily(session, target)
                await session.commit()
            logger.info(f"[报表] 已聚合 {target} 报表，写入 {n} 行")
        except asyncio.CancelledError:
            logger.info("[报表] 报表聚合任务收到取消信号，退出")
            break
        except Exception as exc:   # noqa: BLE001
            logger.error(f"[报表] 聚合失败：{exc}")
            await asyncio.sleep(60)
