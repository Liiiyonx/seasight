"""数据库会话管理。

使用 SQLAlchemy 2.0 异步接口，会话生命周期由依赖注入管理。

★ 引擎是**惰性创建**的，这是刻意的设计：
   若在模块导入时就 `create_async_engine(...)`，那么任何 `import` 了
   模型（模型要 import Base）的代码都会连带要求数据库驱动存在 ——
   于是「只想跑一个纯逻辑单元测试」也得先装 asyncpg，
   测试环境被迫背上一整套数据库依赖。
   惰性化之后，模型定义与逻辑测试可以在无驱动的环境下跑，
   真正要连库时（应用启动/请求进来）才建引擎。
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


# ---------- 异步引擎（惰性单例） ----------
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """返回全局异步引擎，首次调用时才真正创建。"""
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.debug and settings.app_env == "development",
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,   # 连接健康检查，防止长连接被数据库断开
            pool_recycle=3600,
            # 让 asyncpg 自己执行连接超时，而不是由 asyncio.wait_for 从外部取消。
            # 否则连接失败时 asyncpg 会留下未 await 的取消协程，
            # 表现为测试/关闭阶段出现 "coroutine 'Connection._cancel' was never awaited"。
            # 不设置全局 command_timeout：那会误杀报表聚合等正常长查询。
            connect_args={"timeout": settings.postgres_connect_timeout_seconds},
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """返回会话工厂，首次调用时才真正创建。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：提供数据库会话，自动提交/回滚。

    用法：
        @router.get("/items")
        async def list_items(session: AsyncSession = Depends(get_session)):
            ...
    """
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """应用关闭时释放连接池。"""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
