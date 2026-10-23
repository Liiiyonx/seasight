"""统计接口（大屏指标卡与类别分布）。

与 reports.py 分开的原因：
stats 服务于「大屏实时」，reports 服务于「治理报表」，
两者的数据源与刷新频率完全不同（前者查明细、后者读预聚合宽表）。

数据完整性（WP-07）契约
------------------------
1. 每个对外指标必须可回答四个问题：口径 / 分母 / 时间窗 / 数据来源。
   `/dashboard` 响应通过 `metrics_meta` 直接携带声明；
   `/classes`、`/trend`、`/notifications` 以本模块的 METRIC 常量
   文档化（保持列表载荷形状，前端兼容）。
2. 没有可靠来源的指标不得伪造数值：本模块所有数字均来自真实仓库查询
   （t_event / t_task / t_device），不新增任何估算字段。
3. 分母为零的比率语义：分母为 0 时返回 None（not_available），不以 0
   冒充实测（见 safe_ratio 与 robots_online_rate）。
4. coverage_area 等报表预聚合字段属于 reports 域，本模块不输出；
   报表口径见 app.services.report.METRIC_DEFINITIONS。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.core.deps import CurrentUser, get_current_user
from app.core.exceptions import ApiResponse
from app.models.event import Event, WasteClass
from app.models.task import Task, TaskStatus
from app.repositories import DeviceRepository, EventRepository, TaskRepository

router = APIRouter()


# ----------------------------------------------------------------------
# 对外指标口径声明（WP-07 单一真源）
# 每个条目必须包含：口径 / 分母 / 时间窗 / 来源（REQUIRED_METRIC_FIELDS）
# ----------------------------------------------------------------------
REQUIRED_METRIC_FIELDS: tuple[str, ...] = ("口径", "分母", "时间窗", "来源")

DASHBOARD_METRICS: dict[str, dict[str, str]] = {
    "event_count_24h": {
        "label": "24 小时事件数",
        "口径": "近 24 小时 t_event 事件计数（operator 角色只统计 township_scope 辖区）",
        "分母": "1（计数，不涉比率）",
        "时间窗": "请求时刻前 24 小时（[window_start, window_end]，以查询执行时刻为准）",
        "来源": "t_event.event_time（EventRepository.count_since）",
        "availability": "measured",
    },
    "pending_tasks": {
        "label": "待派单工单数",
        "口径": "当前状态为 pending 的 t_task 计数（状态快照，非时间窗）",
        "分母": "1（计数，不涉比率）",
        "时间窗": "请求时刻状态快照（全量，不按时间窗过滤）",
        "来源": "t_task.status（TaskRepository.count_by_status）",
        "availability": "measured",
    },
    "collecting_tasks": {
        "label": "作业中工单数",
        "口径": "当前状态为 collecting 或 navigating 的 t_task 计数（状态快照）",
        "分母": "1（计数，不涉比率）",
        "时间窗": "请求时刻状态快照（全量，不按时间窗过滤）",
        "来源": "t_task.status（TaskRepository.count_by_status）",
        "availability": "measured",
    },
    "done_tasks_24h": {
        "label": "24 小时完成工单数",
        "口径": "近 24 小时状态为 done 的工单计数（按 Task.finished_at 判定）",
        "分母": "1（计数，不涉比率）",
        "时间窗": "请求时刻前 24 小时（按 Task.finished_at）",
        "来源": "t_task.status + finished_at（TaskRepository.done_count_since）",
        "availability": "measured",
    },
    "robots_online": {
        "label": "在线机器人数量",
        "口径": "device_type=robot 且 status=online 的设备计数",
        "分母": "1（计数，不涉比率）",
        "时间窗": "请求时刻状态快照（以设备心跳状态为准）",
        "来源": "t_device（DeviceRepository.list_robots）",
        "availability": "measured",
    },
    "robots_total": {
        "label": "机器人总数",
        "口径": "device_type=robot 的设备总数",
        "分母": "1（计数，不涉比率）",
        "时间窗": "请求时刻状态快照",
        "来源": "t_device（DeviceRepository.list_robots）",
        "availability": "measured",
    },
    "robots_online_rate": {
        "label": "机器人在线率",
        "口径": "robots_online / robots_total（在线机器人占全部机器人的比例）",
        "分母": "robots_total；分母为 0 时返回 null（not_available），不以 0 冒充实测",
        "时间窗": "请求时刻状态快照",
        "来源": "由 robots_online 与 robots_total 派生（不新增数据源）",
        "availability": "derived；分母为 0 时运行时返回 null（not_available），null 透传不转 0",
    },
    "collected_kg_total": {
        "label": "累计清理量",
        "口径": "t_task.collected_weight 全表合计（含历史所有工单，不按状态/时间窗过滤）",
        "分母": "1（计量合计，不涉比率）",
        "时间窗": "全量累计（非时间窗）",
        "来源": "t_task.collected_weight（TaskRepository.sum_collected_weight）",
        "availability": "measured",
    },
    "devices_online": {
        "label": "在线设备数",
        "口径": "status=online 的设备计数（含机器人、岸端相机等全部设备类型）",
        "分母": "1（计数，不涉比率）",
        "时间窗": "请求时刻状态快照",
        "来源": "t_device（DeviceRepository.count_by_status）",
        "availability": "measured",
    },
    "devices_total": {
        "label": "设备总数",
        "口径": "全部设备计数（各状态之和）",
        "分母": "1（计数，不涉比率）",
        "时间窗": "请求时刻状态快照",
        "来源": "t_device（DeviceRepository.count_by_status）",
        "availability": "measured",
    },
}

CLASS_DISTRIBUTION_METRIC: dict[str, str] = {
    "label": "事件类别分布",
    "口径": "近 N 小时 t_event 按 main_class 计数（饼图数据源；operator 只统计本辖区）",
    "分母": "1（计数，不涉比率）",
    "时间窗": "请求时刻前 N 小时（hours 参数，默认 24，上限 720）",
    "来源": "t_event.main_class（明细查询，非预聚合宽表）",
}

EVENT_TREND_METRIC: dict[str, str] = {
    "label": "事件趋势（按小时）",
    "口径": "近 N 小时 t_event 按小时桶（date_trunc('hour')）计数（折线图数据源）",
    "分母": "1（计数，不涉比率）",
    "时间窗": "请求时刻前 N 小时（hours 参数，默认 24，上限 168）",
    "来源": "t_event.event_time（明细查询）",
}

NOTIFICATIONS_METRIC: dict[str, str] = {
    "label": "站内通知时间线",
    "口径": "近 N 小时 t_event 与 t_task 合并时间线（告警 + 工单动态），按时间倒序截取 limit 条",
    "分母": "1（计数，不涉比率）",
    "时间窗": "事件按 event_time、工单按 updated_at，均在请求时刻前 N 小时（hours 参数，默认 24）",
    "来源": "t_event / t_task（明细查询，非独立通知表）",
}


def safe_ratio(numerator: float | int, denominator: float | int) -> float | None:
    """分子 / 分母的比率；分母为 0 时返回 None（not_available），不以 0 冒充实测。

    区分两种「0」：
    - 分母 > 0、分子 = 0 → 0.0（真实 0）；
    - 分母 = 0           → None（未统计，比率无定义）。
    返回值四舍五入到 4 位小数；纯函数，相同输入 → 相同输出。
    """
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


@router.get("/dashboard", summary="大屏顶部指标卡")
async def dashboard_stats(
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """大屏实时指标：事件数、工单数、机器人状态、累计清理量。

    operator 角色只统计本辖区（township_scope），admin/viewer 看全部。

    数据完整性（WP-07）：所有数值均来自真实仓库查询；每个指标的
    口径 / 分母 / 时间窗 / 来源见响应 `metrics_meta`；时间窗见
    `window_start` / `window_end`。
    """
    event_repo = EventRepository(session)
    task_repo = TaskRepository(session)
    device_repo = DeviceRepository(session)

    township = user.township_scope if user.role == "operator" else None
    now = datetime.now()
    window_start = now - timedelta(hours=24)

    event_count_24h = await event_repo.count_since(hours=24, township=township)
    task_counts = await task_repo.count_by_status(township=township)
    device_counts = await device_repo.count_by_status()
    collected = await task_repo.sum_collected_weight(township=township)
    done_24h = await task_repo.done_count_since(hours=24, township=township)

    robots = await device_repo.list_robots()
    robots_online = sum(1 for d in robots if d.status == "online")
    robots_total = len(robots)

    return ApiResponse.ok(
        {
            "event_count_24h": event_count_24h,
            "pending_tasks": task_counts.get("pending", 0),
            "collecting_tasks": task_counts.get("collecting", 0)
            + task_counts.get("navigating", 0),
            "done_tasks_24h": done_24h,
            "robots_online": robots_online,
            "robots_total": robots_total,
            "robots_online_rate": safe_ratio(robots_online, robots_total),
            "collected_kg_total": round(collected, 2),
            "devices_online": device_counts.get("online", 0),
            "devices_total": sum(device_counts.values()),
            # WP-07：指标口径声明（口径 / 分母 / 时间窗 / 来源）
            "metrics_meta": DASHBOARD_METRICS,
            # WP-07：显式时间窗（名义窗口；仓库层以各自查询时刻为准）
            "window_start": window_start.isoformat(timespec="seconds"),
            "window_end": now.isoformat(timespec="seconds"),
        }
    )


@router.get("/classes", summary="类别分布统计")
async def class_distribution(
    hours: int = Query(24, ge=1, le=720),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """按垃圾类别统计事件数（饼图数据源）。

    数据完整性（WP-07）：口径 / 分母 / 时间窗 / 来源见
    `CLASS_DISTRIBUTION_METRIC`。
    """
    since = datetime.now() - timedelta(hours=hours)
    township = user.township_scope if user.role == "operator" else None
    conditions = [Event.event_time >= since]
    if township:
        conditions.append(Event.township == township)
    stmt = (
        select(Event.main_class, func.count().label("cnt"))
        .where(*conditions)
        .group_by(Event.main_class)
        .order_by(func.count().desc())
    )
    rows = await session.execute(stmt)

    data = [
        {
            "main_class": row[0],
            "label": WasteClass.LABELS.get(row[0], row[0]),
            "count": int(row[1]),
        }
        for row in rows.all()
    ]
    return ApiResponse.ok(data)


@router.get("/trend", summary="事件趋势（按小时）")
async def event_trend(
    hours: int = Query(24, ge=1, le=168),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    """近 N 小时事件趋势折线图数据源。

    数据完整性（WP-07）：口径 / 分母 / 时间窗 / 来源见 `EVENT_TREND_METRIC`。
    """
    since = datetime.now() - timedelta(hours=hours)
    township = user.township_scope if user.role == "operator" else None
    bucket = func.date_trunc("hour", Event.event_time).label("bucket")

    conditions = [Event.event_time >= since]
    if township:
        conditions.append(Event.township == township)
    stmt = (
        select(bucket, func.count().label("cnt"))
        .where(*conditions)
        .group_by(bucket)
        .order_by(bucket)
    )
    rows = await session.execute(stmt)

    return ApiResponse.ok(
        [
            {"time": row[0].isoformat() if row[0] else None, "count": int(row[1])}
            for row in rows.all()
        ]
    )


@router.get("/notifications", summary="站内通知（最近告警 + 工单动态）")
async def notifications(
    hours: int = Query(24, ge=1, le=720),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    """最近告警与工单动态的合并时间线，供顶栏通知铃铛使用。

    数据来自明细表（t_event / t_task），不是独立通知表——避免为「已读」
    状态引入一套新表；铃铛的未读判断由前端用「上次查看时间」本地完成。

    数据完整性（WP-07）：口径 / 分母 / 时间窗 / 来源见
    `NOTIFICATIONS_METRIC`。
    """
    since = datetime.now() - timedelta(hours=hours)

    ev_rows = await session.execute(
        select(
            Event.event_id,
            Event.main_class,
            Event.event_time,
            Event.device_id,
            Event.status,
        )
        .where(Event.event_time >= since)
        .order_by(Event.event_time.desc())
        .limit(limit)
    )

    tk_rows = await session.execute(
        select(
            Task.task_id,
            Task.status,
            Task.updated_at,
            Task.robot_id,
        )
        .where(Task.updated_at >= since)
        .order_by(Task.updated_at.desc())
        .limit(limit)
    )

    items: list[dict] = []
    for event_id, main_class, event_time, device_id, status in ev_rows.all():
        label = WasteClass.LABELS.get(main_class, main_class)
        items.append({
            "type": "event",
            "event_id": event_id,
            "main_class": main_class,
            "main_class_label": label,
            "title": f"识别到{label}，已进入事件中心",
            "time": event_time.isoformat() if event_time else None,
            "device_id": device_id,
            "status": status,
        })
    for task_id, status, updated_at, robot_id in tk_rows.all():
        items.append({
            "type": "task",
            "task_id": task_id,
            "status": status,
            "status_label": TaskStatus.LABELS.get(status, status),
            "title": f"工单 {task_id} 状态更新为 {TaskStatus.LABELS.get(status, status)}",
            "time": updated_at.isoformat() if updated_at else None,
            "robot_id": robot_id,
        })

    items.sort(key=lambda x: x["time"] or "", reverse=True)
    return ApiResponse.ok(items[:limit])
