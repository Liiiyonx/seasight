"""Alembic 运行环境（同步引擎）。

★ 为什么这里用**同步**驱动，而应用跑的是异步？
   Alembic 的迁移执行流程是同步的（它自己控制事务边界），
   强行套异步会引入一堆 `asyncio.run` 与事件循环冲突。
   官方推荐做法就是：应用用 asyncpg，迁移用 psycopg2 ——
   两条独立的连接串，互不干扰。

   所以 `app.core.config.settings` 里同时提供了：
       database_url        → 异步（postgresql+asyncpg://...）
       database_url_sync   → 同步（postgresql+psycopg2://...）

★ 连接串不写在 alembic.ini 里 —— 那个文件进 git，写死密码就是泄露。
   这里从应用配置读，配置再读环境变量，链条单一可控。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 把 backend/ 加入 sys.path，保证能 import app.*
# （alembic 从 backend/ 目录执行时这本已成立，但 CI 里
#   可能从仓库根目录调 `alembic -c backend/alembic.ini`，
#   所以这里显式兜一下。）
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402

# Alembic 自身的日志配置
config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False：alembic 在进程内跑（测试/集成）时
    # 不得禁用应用已有日志器（默认 True 会把 seasight.* 禁用，导致其后
    # caplog 收不到记录——测试顺序依赖缺陷的根因）。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# 把运行时算出来的同步连接串注入配置
config.set_main_option("sqlalchemy.url", settings.database_url_sync)

# ----------------------------------------------------------------------
# 目标元数据：autogenerate 靠它对比"模型定义"与"数据库现状"
#
# ★ 必须 import 所有模型模块，否则它们没注册到 Base.metadata 上，
#   autogenerate 会认为"这些表不存在"并生成 DROP TABLE。
#   新增模型文件时记得回来补一行 —— 这是最容易踩的坑。
# ----------------------------------------------------------------------
from app.db.session import Base  # noqa: E402
import app.models.device  # noqa: F401,E402
import app.models.event  # noqa: F401,E402
import app.models.misc  # noqa: F401,E402
import app.models.task  # noqa: F401,E402
# WP-02 集成：注册 Agent Runtime 四表（t_agent_run/step/memory/approval），
# 否则 autogenerate 会把它们误判为"应删除的表"。
import app.models.agent  # noqa: F401,E402
# WP-10 集成：注册持久化续跑状态表 t_agent_run_state。
import app.models.agent_state  # noqa: F401,E402
# WP-14D 集成：注册 ACK 审计账本表 t_task_ack。
import app.models.task_ack  # noqa: F401,E402
# Huawei/Nexent knowledge domain: asset, ontology, and evidence tables.
import app.models.knowledge  # noqa: F401,E402
# 对话助手集成：注册会话/消息两表（t_chat_session / t_chat_message）。
import app.models.chat  # noqa: F401,E402

target_metadata = Base.metadata


def _include_object(obj, name, type_, reflected, compare_to) -> bool:
    """过滤掉不该被 Alembic 管理的对象。

    ★ 三类必须排除，否则 autogenerate 会生成破坏性迁移：
      1. PostGIS 系统表（spatial_ref_sys 等）—— 删了 PostGIS 就废了
      2. 分区表子表（t_event_2026_09 这类）—— 由 SQL 脚本按月建，
         Alembic 不该插手
      3. Alembic 自己的版本表
    """
    if type_ == "table":
        ignored_prefixes = (
            "spatial_ref_sys",
            "geography_columns",
            "geometry_columns",
            "raster_",
            "topology",
            "layer",
        )
        if name.startswith(ignored_prefixes):
            return False
        # 分区子表：主表 t_event / t_track 由迁移管理，
        # t_event_YYYY_MM 与 t_track_YYYYMMDD 子表交给 SQL 脚本
        if name.startswith(("t_event_", "t_track_")) and name not in ("t_event", "t_track"):
            return False
    return True


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。

    用途：把迁移脚本交给 DBA 审核后再执行（政务项目常见要求）。
        alembic upgrade head --sql > migration.sql
    """
    context.configure(
        url=settings.database_url_sync,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=_include_object,
        compare_type=True,
        compare_server_default=True,
        # 已知限制（alembic 1.14）：表注释比较器无条件注册，无关闭开关；
        # ORM 全项目不声明 comment，而 01_schema.sql 有 17 处 COMMENT，
        # 故 autogenerate/check 会为这些表生成 remove_table_comment 噪音。
        # 属既有行为（业务表同样命中），不影响 upgrade/downgrade；
        # 生成迁移时按 Makefile 提示人工剪掉注释类操作即可。
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,   # 迁移是一次性任务，不需要连接池
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
            # 这两项让 autogenerate 能发现字段类型变更与默认值变更，
            # 否则改字段类型时它什么都不报，迁移就漏了。
            compare_type=True,
            compare_server_default=True,
            # 已知限制（alembic 1.14）：表注释比较器无条件注册，无关闭开关；
            # ORM 全项目不声明 comment，而 01_schema.sql 有 17 处 COMMENT，
            # 故 autogenerate/check 会为这些表生成 remove_table_comment 噪音。
            # 属既有行为（业务表同样命中），不影响 upgrade/downgrade；
            # 生成迁移时按 Makefile 提示人工剪掉注释类操作即可。
            # 把事务隔离级别交给数据库默认，避免长事务锁表
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
