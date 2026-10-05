"""报表接口（治理量化报表）。

数据源为预聚合宽表 t_report_daily —— 由定时任务每日凌晨生成，
报表页只读宽表，即使事件量到百万级仍毫秒响应。

数据完整性（WP-07）契约：
- `coverage_area` 可空：NULL = 未统计（not_available），**不转成 0**；
  真实 0（available/partial 且合计为 0）与未统计在结构上不同。
- 每个对外指标携带 `coverage_availability` 三态
  （available / partial / not_available，判定见 app.services.report）。
- CSV 导出把 NULL 留空（""），不得写成 0。
"""

from __future__ import annotations

import csv
import io
from datetime import date, date as _date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.downloads import attachment_header
from app.core.exceptions import ApiResponse
from app.db.session import get_session
from app.models.event import WasteClass
from app.models.misc import ReportDaily
from app.services.report import coverage_summary_availability

router = APIRouter()


def coverage_area_out(value: Decimal | float | None) -> float | None:
    """报表响应层 coverage_area 的空值透传：None → None（不转 0）。"""
    return None if value is None else float(value)


def coverage_csv_cell(value: Decimal | float | None) -> str:
    """CSV 导出单元格：NULL → ""（留空，不写 0）；有值 → 两位小数。"""
    if value is None:
        return ""
    return f"{float(value):.2f}"


class AggregateRequest(BaseModel):
    """手动触发报表聚合的请求体。

    target_date 缺省为今天；传 YYYY-MM-DD 可回溯聚合历史某天
    （例如补跑、或验证聚合与 seed 口径一致）。
    """

    target_date: str | None = None


@router.get("/daily", summary="日报表查询")
async def daily_report(
    days: int = Query(7, ge=1, le=90),
    township: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
):
    """查询日报表（读预聚合宽表，毫秒响应）。"""
    since = date.today() - timedelta(days=days)
    conditions = [ReportDaily.stat_date >= since]
    if township:
        conditions.append(ReportDaily.township == township)

    stmt = (
        select(ReportDaily)
        .where(and_(*conditions))
        .order_by(ReportDaily.stat_date.desc())
        .limit(500)
    )
    rows = await session.execute(stmt)

    items = [
        {
            "stat_date": str(r.stat_date),
            "township": r.township,
            "main_class": r.main_class,
            "main_class_label": WasteClass.LABELS.get(r.main_class or "", ""),
            "event_count": r.event_count,
            "task_count": r.task_count,
            "done_count": r.done_count,
            "collected_kg": float(r.collected_kg),
            # WP-07：未统计(None) 透传为 null，不转 0；可用性三态随行输出
            "coverage_area": coverage_area_out(r.coverage_area),
            "coverage_availability": r.coverage_availability or "not_available",
        }
        for r in rows.scalars().all()
    ]
    return ApiResponse.ok(items)


@router.get("/summary", summary="报表汇总（按乡镇聚合）")
async def report_summary(
    days: int = Query(7, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
):
    """按乡镇聚合的治理成果汇总（报表页顶部卡片）。

    覆盖面积只对非 NULL 行求和；可用性按「有实测行数 / 总行数」判定
    （available / partial / not_available，见 coverage_summary_availability）。
    """
    since = _date.today() - timedelta(days=days)
    stmt = (
        select(
            ReportDaily.township,
            func.sum(ReportDaily.event_count).label("events"),
            func.sum(ReportDaily.done_count).label("done"),
            func.sum(ReportDaily.collected_kg).label("kg"),
            func.sum(ReportDaily.coverage_area).label("area"),
            func.count(ReportDaily.coverage_area).label("measured_rows"),
            func.count().label("total_rows"),
        )
        .where(ReportDaily.stat_date >= since)
        .group_by(ReportDaily.township)
        .order_by(func.sum(ReportDaily.event_count).desc())
    )
    rows = await session.execute(stmt)

    items = [
        {
            "township": r.township or "未标注",
            "event_count": int(r.events or 0),
            "done_count": int(r.done or 0),
            "collected_kg": round(float(r.kg or 0), 2),
            # WP-07：全 NULL 时 area 为 None → 透传 null，不转 0
            "coverage_area": coverage_area_out(r.area),
            "coverage_availability": coverage_summary_availability(
                int(r.measured_rows or 0), int(r.total_rows or 0)
            ),
        }
        for r in rows.all()
    ]
    return ApiResponse.ok(items)


@router.post("/aggregate", summary="手动触发报表聚合")
async def aggregate_now(
    payload: AggregateRequest | None = None,
    session: AsyncSession = Depends(get_session),
):
    """把明细表（t_event / t_task）聚合进 t_report_daily。

    定时任务每日凌晨自动跑；本接口用于手动触发（演示、补跑、验证）。
    幂等：重复聚合同一日期会 UPSERT 覆盖，不会产生重复行。
    """
    from app.services.report import aggregate_daily

    target = date.today()
    if payload and payload.target_date:
        try:
            target = datetime.strptime(payload.target_date, "%Y-%m-%d").date()
        except ValueError:
            return ApiResponse.fail(code=4002, message="target_date 格式应为 YYYY-MM-DD")

    rows = await aggregate_daily(session, target)
    await session.commit()
    return ApiResponse.ok(
        {"stat_date": str(target), "rows": rows},
        message=f"已聚合 {target} 报表，写入/更新 {rows} 行",
    )


@router.get("/export", summary="导出日报表 CSV")
async def export_report(
    days: int = Query(30, ge=1, le=90),
    township: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
):
    """导出治理日报表为 CSV（带 UTF-8 BOM，Excel 打开中文不乱码）。

    WP-07：覆盖面积为 NULL（未统计）时导出**留空**，不写成 0。
    """
    since = date.today() - timedelta(days=days)
    conditions = [ReportDaily.stat_date >= since]
    if township:
        conditions.append(ReportDaily.township == township)

    stmt = (
        select(ReportDaily)
        .where(and_(*conditions))
        .order_by(ReportDaily.stat_date.desc(), ReportDaily.township)
    )
    rows = (await session.execute(stmt)).scalars().all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["统计日期", "乡镇", "垃圾类别", "事件数", "工单数", "完成数", "清理量(kg)", "覆盖面积(㎡)", "覆盖可用性"])
    for r in rows:
        w.writerow([
            str(r.stat_date),
            r.township or "",
            WasteClass.LABELS.get(r.main_class or "", "") or (r.main_class or ""),
            r.event_count,
            r.task_count,
            r.done_count,
            f"{float(r.collected_kg):.2f}",
            coverage_csv_cell(r.coverage_area),
            r.coverage_availability or "not_available",
        ])

    content = "\ufeff" + buf.getvalue()   # UTF-8 BOM，否则 Excel 中文乱码
    stamp = date.today().isoformat()
    filename = f"治理日报_{stamp}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": attachment_header(
                filename,
                ascii_fallback=f"seasight_report_{stamp}.csv",
            )
        },
    )
