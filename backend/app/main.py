"""探海灵眸 Oceanus — 后端应用入口。

启动流程（lifespan）：
    1. 初始化日志
    2. 连接 Redis
    3. 启动 MQTT 订阅
    4. ACK 判重水位恢复（WP-14E：MQTT 客户端可用后从 t_task_ack 重建，
       失败/超时降级继续启动，结果写入 app.state 供 /health 观测）
    5. 启动派单消费者（Redis Streams）
    6. 启动定时任务（补派、报表聚合）
    关闭时逆序释放资源。
"""

from __future__ import annotations

import asyncio
import sys
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

# ★ Windows 上 aiomqtt（基于 paho-mqtt）依赖事件循环的 add_reader/add_writer，
#   而 Windows 默认的 ProactorEventLoop 不支持这两个 API（抛 NotImplementedError），
#   导致 MQTT 客户端永远连不上、表现为「Operation timed out」。
#   必须在任何事件循环创建之前（uvicorn 启动前）切到 Selector 事件循环。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from app.api.v1 import api_router
from app.core.config import settings, validate_production_settings
from app.core.deps import close_redis, get_redis
from app.db.session import dispose_engine
from app.middleware.response import register_exception_handlers, trace_id_middleware


#: ACK 判重水位启动恢复的超时上界（秒）。
#: 恢复来自 t_task_ack 的规范回执与设备 ACK 水位；数据库不可用 / 表缺失 /
#: 查询异常一律降级为 warning 继续启动，绝不让启动无限期挂起或失败。
ACK_TRACKER_REBUILD_TIMEOUT_SECONDS = 5.0


async def recover_ack_tracker(
    tracker: Any,
    *,
    session_factory: Any = None,
    timeout: float = ACK_TRACKER_REBUILD_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """启动阶段从 ``t_task_ack`` 恢复 ACK 判重水位（WP-14E）。

    ``tracker`` 为平台侧 :class:`app.mqtt.ack.AckTracker`（MQTT 客户端实例
    持有）。实现即调用
    ``TaskAckRepository.rebuild_ack_tracker(tracker)``（WP-14D 能力方法）。

    恢复边界（如实声明，不伪装完整恢复）：
    - ``duplicate`` / ``out_of_order`` 判定重启后**可恢复**（规范回执 +
      设备 ACK 水位来自账本）；
    - ``late`` 判定依赖发布时登记的 ``expires_at``（t_task_ack 不存）——
      「发布后未 ACK」的命令重启后**仍不可判 late**（内存登记表固有边界）。

    四项要求：
    1. **降级不失败**：数据库不可用 / 表不存在 / 查询异常 → warning 并
       继续启动，绝不抛异常；
    2. **幂等**：重复启动不叠加、不报错（``rebuild_ack_tracker`` 对已存在
       的规范回执只判重不覆盖，水位取 max）；
    3. **耗时上界**：整个重建包在 ``asyncio.wait_for`` 内，超时即放弃并降级；
    4. **可观测**：返回 ``{status, rows, elapsed_ms, detail}``，由调用方
       写入 ``app.state.ack_tracker_recovery`` 供 ``/health`` 展示，同时打日志。

    ``session_factory`` 可注入（测试用）；缺省用全局 ``get_session_factory``。
    """
    result: dict[str, Any] = {
        "status": "ok",
        "rows": 0,
        "elapsed_ms": 0.0,
        "detail": "",
    }
    if tracker is None:
        result.update(
            status="degraded",
            detail="ack_tracker 不可用（MQTT 客户端未启动），跳过判重水位恢复",
        )
        logger.warning(f"[启动] ACK 判重水位恢复降级：{result['detail']}")
        return result

    if session_factory is None:
        from app.db.session import get_session_factory as _default_factory

        session_factory = _default_factory

    started = time.monotonic()

    async def _rebuild() -> int:
        async with session_factory()() as session:
            from app.repositories import TaskAckRepository

            return await TaskAckRepository(session).rebuild_ack_tracker(tracker)

    try:
        rows = await asyncio.wait_for(_rebuild(), timeout=timeout)
        result["rows"] = int(rows)
        result["detail"] = f"从 t_task_ack 重建 {rows} 条规范回执"
        logger.info(f"[启动] ACK 判重水位恢复：{result['rows']} 条规范回执已重建")
    except asyncio.TimeoutError:
        result.update(
            status="degraded",
            detail=f"重建超过上界 {timeout}s（已取消），降级继续启动",
        )
        logger.warning(f"[启动] ACK 判重水位恢复超时（>{timeout}s），降级继续启动")
    except Exception as exc:   # noqa: BLE001
        result.update(
            status="degraded",
            detail=f"{type(exc).__name__}: {exc}",
        )
        logger.warning(f"[启动] ACK 判重水位恢复失败（{exc}），降级继续启动")
    finally:
        result["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 3)
    return result


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """应用生命周期管理。"""
    validate_production_settings(settings)
    logger.info(f"[启动] {settings.app_name} 环境={settings.app_env}")

    # ---------- 1. Redis ----------
    try:
        redis = await get_redis()
        await redis.ping()
        logger.info(f"[启动] Redis 已连接 {settings.redis_host}:{settings.redis_port}")
    except Exception as exc:   # noqa: BLE001
        await close_redis()
        logger.warning(f"[启动] Redis 连接失败（{exc}），队列功能不可用")

    # ---------- 2. MQTT 订阅 ----------
    # ★ import 必须放在 try 里：把 import 留在 try 外面，就等于宣称
    #   「aiomqtt 缺失时能降级」但实际会直接崩在启动阶段 ——
    #   降级路径必须把「依赖不存在」和「依赖存在但连不上」一视同仁。
    mqtt_client = None
    try:
        from app.mqtt.client import mqtt_client
        from app.mqtt.handlers import HANDLERS

        await mqtt_client.start(HANDLERS)
    except Exception as exc:   # noqa: BLE001
        logger.warning(f"[启动] MQTT 启动失败（{exc}），设备上报通道不可用")

    # ---------- 2.5 ACK 判重水位恢复（WP-14E） ----------
    # MQTT 客户端可用后，从 t_task_ack 重建 AckTracker 判重水位（duplicate /
    # out_of_order 判定重启后可恢复）。数据库不可用 / 表缺失 / 查询异常 →
    # 降级 warning 继续启动；恢复有超时上界；结果写入 app.state 供 /health
    # 观测，不得静默。
    ack_recovery_result = None
    if mqtt_client is not None:
        try:
            ack_recovery_result = await recover_ack_tracker(mqtt_client.ack_tracker)
        except Exception as exc:   # noqa: BLE001
            ack_recovery_result = {
                "status": "degraded",
                "rows": 0,
                "elapsed_ms": 0.0,
                "detail": f"{type(exc).__name__}: {exc}",
            }
            logger.warning(f"[启动] ACK 判重水位恢复异常（{exc}），降级继续启动")
    app.state.ack_tracker_recovery = ack_recovery_result

    # ---------- 3. 后台任务（派单消费者 + 补派定时器 + 报表聚合） ----------
    background_tasks: list[asyncio.Task[None]] = []
    if settings.background_workers_enabled:
        try:
            from app.services.consumer import dispatch_consumer, pending_dispatcher
            from app.services.report import daily_report_worker

            background_tasks.append(asyncio.create_task(dispatch_consumer()))
            background_tasks.append(asyncio.create_task(pending_dispatcher()))
            background_tasks.append(asyncio.create_task(daily_report_worker()))
            logger.info("[启动] 后台任务已启动（派单消费者 + 补派定时器 + 报表聚合）")
        except Exception as exc:   # noqa: BLE001
            logger.warning(f"[启动] 后台任务启动失败（{exc}）")
    else:
        logger.info("[启动] 后台任务已按配置关闭（BACKGROUND_WORKERS_ENABLED=false）")

    logger.info(f"[启动] 完成，接口文档：http://localhost:8000/docs")

    yield

    # ---------- 关闭 ----------
    # 每步都独立 try：启动阶段降级掉的组件（mqtt_client 可能是 None），
    # 关闭时不能反过来把进程打崩。
    logger.info("[关闭] 正在释放资源...")
    for task in background_tasks:
        task.cancel()
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)

    try:
        from app.services.sim import simulation_manager

        await simulation_manager.stop_all()
    except Exception as exc:   # noqa: BLE001
        logger.warning(f"[关闭] 仿真引擎释放异常（{exc}）")

    if mqtt_client is not None:
        try:
            await mqtt_client.stop()
        except Exception as exc:   # noqa: BLE001
            logger.warning(f"[关闭] MQTT 释放异常（{exc}）")

    for name, closer in (("Redis", close_redis), ("数据库引擎", dispose_engine)):
        try:
            await closer()
        except Exception as exc:   # noqa: BLE001
            logger.warning(f"[关闭] {name} 释放异常（{exc}）")

    logger.info("[关闭] 完成")


def create_app() -> FastAPI:
    """创建 FastAPI 应用。"""
    docs_enabled = settings.app_env != "production"
    app = FastAPI(
        title="探海灵眸 Oceanus API",
        description=(
            "海漂垃圾「感知—决策—执行」全链路智能治理系统 · 后端接口\n\n"
            "**平台是中枢，不是显示屏** —— 识别到垃圾不是弹框，"
            "而是产生事件 → 触发派单 → 形成可追溯工单。"
        ),
        version="1.0.0",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=lifespan,
    )

    # ---------- 中间件 ----------
    app.middleware("http")(trace_id_middleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---------- 异常处理 ----------
    register_exception_handlers(app)

    # ---------- 路由 ----------
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    async def probe_dependencies() -> dict[str, str]:
        """探测运行依赖，供 liveness 与 readiness 共用。"""
        deps: dict[str, str] = {}

        # Redis
        try:
            redis = await get_redis()
            await asyncio.wait_for(redis.ping(), timeout=2.0)
            deps["redis"] = "ok"
        except Exception as exc:   # noqa: BLE001
            await close_redis()
            deps["redis"] = f"down: {type(exc).__name__}"

        # MQTT（只看客户端状态，不做网络探测）
        try:
            from app.mqtt.client import mqtt_client

            deps["mqtt"] = "ok" if mqtt_client.is_connected else "down"
        except Exception as exc:   # noqa: BLE001
            deps["mqtt"] = f"down: {type(exc).__name__}"

        # 数据库
        try:
            from sqlalchemy import text

            from app.db.session import get_session_factory

            async with get_session_factory()() as session:
                # asyncpg 的连接超时由 get_engine() 的 connect_args 控制。
                # 这里不要再用 wait_for 从外部取消：那会触发 asyncpg 的
                # Connection._cancel 清理协程，留下未 await 的 RuntimeWarning。
                await session.execute(text("SELECT 1"))
            deps["database"] = "ok"
        except Exception as exc:   # noqa: BLE001
            deps["database"] = f"down: {type(exc).__name__}"

        return deps

    # ---------- 健康检查 ----------
    @app.get("/health", tags=["系统"], summary="存活检查")
    async def health() -> dict:
        """存活检查 —— 必须反映**真实降级状态**。

        ``/health`` 保持 HTTP 200，便于观测部分依赖故障时的系统状态；
        编排系统请使用 ``/ready`` 决定是否接流量。
        """
        from app.ws.manager import ws_manager

        deps = await probe_dependencies()
        bad = [k for k, v in deps.items() if v != "ok"]
        status = "ok" if not bad else ("degraded" if len(bad) < len(deps) else "down")

        # ACK 判重水位恢复结果（WP-14E）：启动时从 t_task_ack 重建的观测值。
        # 顶层字段、不进入 dependencies —— 不影响三档 status 语义（既有
        # 契约测试锁定 dependencies 只含 redis/mqtt/database）。
        ack_recovery = getattr(app.state, "ack_tracker_recovery", None)

        return {
            "status": status,
            "app": settings.app_name,
            "env": settings.app_env,
            "ws_connections": ws_manager.connection_count,
            "dependencies": deps,
            "ack_recovery": ack_recovery,
        }

    @app.get("/ready", tags=["系统"], summary="就绪检查")
    async def ready() -> JSONResponse:
        """就绪检查：任一依赖不可用时返回 HTTP 503。"""
        deps = await probe_dependencies()
        bad = [k for k, v in deps.items() if v != "ok"]
        status = "ok" if not bad else ("degraded" if len(bad) < len(deps) else "down")
        payload = {
            "status": status,
            "app": settings.app_name,
            "env": settings.app_env,
            "dependencies": deps,
        }
        return JSONResponse(payload, status_code=200 if not bad else 503)

    @app.get("/", tags=["系统"], summary="服务信息")
    async def root() -> dict:
        return {
            "name": settings.app_name,
            "description": "海漂垃圾智能治理系统后端",
            "docs": "/docs" if docs_enabled else None,
            "api_prefix": settings.api_v1_prefix,
        }

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
