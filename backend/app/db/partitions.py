"""t_track 按日分区的自愈。

## 为什么需要这个模块

``t_track`` 是 ``PARTITION BY RANGE (recorded_at)`` 的声明式分区表 ——
**父表自己不存数据**，每条记录必须落进一个覆盖其 ``recorded_at`` 的子分区。
没有对应日期的分区时，Postgres 会直接拒绝 INSERT：

    asyncpg.exceptions.CheckViolationError:
    no partition of relation "t_track" found for row
    DETAIL: Partition key of the failing row contains (recorded_at) = (...+08)

这条错误在 2026-09-27 的线上验收里把「人工建单 → 点仿真」直接打成红色「异常」，
而演示种子工单（更早的日期，分区当初建出来了）却一切正常 ——
所以现象看起来像"新工单坏了"，实际是日期滚出了初始化窗口。

## 缺口在哪

分区只在数据库**首次初始化**时由 ``db/init/01_schema.sql`` 建出
「当日 -1 ~ 当日 +7」共 9 个，之后没有任何东西会补。
配套脚本 ``deploy/postgres/ensure_track_partitions.sql`` 写得很清楚
（"Run daily from cron/systemd, or schedule it through pg_cron"），
但全仓没有调用点：没有 cron、compose 里没有 init 钩子、也没配 pg_partman
（``01_schema.sql`` 的注释里写着"生产环境用 pg_partman 自动管理"，属于有意向未落地）。

## 这里的做法

**不把正确性押在"运维记得挂定时任务"上** —— 让写入路径自己保证分区存在：
缺就建、幂等（``CREATE TABLE IF NOT EXISTS``），代价是每进程一次系统表查询。

进程内用 ``_known`` 缓存已确认存在的日期，所以稳态下零额外开销。
多实例并存最坏情况是并发建同一个分区，而 ``IF NOT EXISTS`` 让那是无害的
（两个事务里只有一个真正建表成功）。
"""

from __future__ import annotations

from datetime import date, timedelta

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

TABLE = "t_track"

# 已确认存在的分区日期（进程内缓存）。
# 分区建出来就不会消失，所以缓存不需要失效策略。
_known: set[date] = set()


def partition_name(day: date) -> str:
    """分区表名，与 01_schema.sql 的 to_char(d,'YYYYMMDD') 保持一致。"""
    return f"{TABLE}_{day:%Y%m%d}"


async def ensure_track_partition(session: AsyncSession, day: date) -> bool:
    """确保 ``day`` 当天的 t_track 分区存在；已存在则直接返回。

    Returns:
        本次调用是否真的建了表（用于日志与测试断言）。

    Raises:
        sqlalchemy 异常会向上抛：调用方（遥测 handler）需要知道分区没建成功，
        否则 INSERT 一样会失败，只是报错点更靠后。
    """
    if day in _known:
        return False

    name = partition_name(day)

    # 用 to_regclass 而不是 information_schema：前者走 OID 直查，
    # 且对"权限不足看不见"和"不存在"给出不同结果，语义更准。
    existing = await session.scalar(
        text("SELECT to_regclass(:qualified)"),
        {"qualified": f"public.{name}"},
    )
    if existing is not None:
        _known.add(day)
        return False

    # 边界是 [day, day+1)，与 01_schema.sql 的 FROM (d) TO (d + 1) 完全一致。
    # 用参数化常量拼 DDL：表名/日期都由本模块生成，不接受外部输入。
    await session.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {TABLE} "
            f"FOR VALUES FROM ('{day.isoformat()}') TO "
            f"('{(day + timedelta(days=1)).isoformat()}')"
        )
    )
    _known.add(day)
    logger.info(f"[partition] t_track 缺分区，已补建 {name}")
    return True


async def ensure_track_partitions_around(session: AsyncSession, days: int = 2) -> list[str]:
    """确保「今天 ~ 今天+days」的分区都存在，返回本次新建的表名。

    跨零点的仿真会把遥测写到第二天，所以往前多铺几天。
    今天取的是**数据库的** CURRENT_DATE 而不是 Python 的 date.today()：
    DEFAULT now() 由数据库计算，只有用它自己的日期才不会因时区差异错位。
    """
    today = await session.scalar(text("SELECT CURRENT_DATE"))
    created: list[str] = []
    for offset in range(days + 1):
        if await ensure_track_partition(session, today + timedelta(days=offset)):
            created.append(partition_name(today + timedelta(days=offset)))
    return created


def reset_cache() -> None:
    """清空进程内缓存（仅供测试使用）。"""
    _known.clear()
