"""baseline: 冻结由 db/init/01_schema.sql 建立的初始 schema

Revision ID: 20260918_1000_baseline
Revises:
Create Date: 2026-09-18 10:00:00

★ 这是一个**空迁移**，故意写成空的 —— 说明为什么：

本项目的初始建表**不由 Alembic 执行**，而是走
`backend/db/init/01_schema.sql`（postgres 容器首次启动时由
docker-entrypoint-initdb.d 自动执行）。原因是初始 schema 里有
三样 Alembic 表达不了或表达起来很别扭的东西：

  1. `CREATE EXTENSION postgis` —— 扩展创建
  2. `t_track` 按月的**分区表**（含 PARTITION OF 子句与默认分区）
  3. 一组触发器函数（自动维护 updated_at、事件分区自动创建）

硬要用 Alembic 表达也能写，但会变成一大坨 `op.execute("原生SQL")`，
既失去 autogenerate 的价值，又平添一层间接。

（顺带一个 Python 语法坑：本 docstring 里不能出现三个连续双引号，
 否则 docstring 会被提前闭合、模块直接 SyntaxError —— 上面那处
 原本想写 op.execute 的三引号字面量，就是踩了这个坑。）

所以采用双轨制：
  · 轨道 A（本 revision 之前）：SQL 脚本建表
  · 轨道 B（本 revision 及之后）：Alembic 做增量变更

本 revision 的作用是**打个桩**：`alembic stamp 20260918_1000_baseline`
之后，Alembic 就知道"上面这些表已经存在了，别再试图创建它们"。
不 stamp 的话，第一次 `revision --autogenerate` 会生成一堆
`op.create_table('t_event', ...)`，一执行就报表已存在。

────────────────────────────────────────────────────────────
新环境初始化步骤（重要，顺序不能反）：

    # 1. 起数据库（会自动跑 01_schema.sql + 02_seed.sql）
    docker compose up -d postgres

    # 2. 把 baseline 标记为已应用（不执行任何 SQL）
    docker compose exec backend alembic stamp head

    # 3. 之后的变更正常走 Alembic
    docker compose exec backend alembic revision --autogenerate -m "描述"
    docker compose exec backend alembic upgrade head

验证是否对齐：
    docker compose exec backend alembic current
    # 应输出 20260918_1000_baseline (head)
────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "20260918_1000_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """空 —— schema 已由 db/init/01_schema.sql 建立。

    刻意不在这里重复建表：
    如果写 create_table 且脚本也建了，就会 duplicate table 报错；
    如果加 IF NOT EXISTS 又能跑通，但那样 Alembic 的记录就失去意义
    （它记录的应该是"我做了什么"，而不是"我尝试做了什么"）。
    """
    pass


def downgrade() -> None:
    """空 —— 不回退基线。

    回退到"建表之前"意味着 DROP 掉全部业务表，那是灾难性操作，
    不应该出现在一个随手敲的 `alembic downgrade base` 里。
    真要清库，用 `make db-reset`（有二次确认）。
    """
    pass
