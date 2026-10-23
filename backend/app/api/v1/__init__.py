"""API v1 路由聚合。"""

from fastapi import APIRouter

from app.api.v1 import (
    agents,
    ai,
    assistant,
    audit,
    auth,
    devices,
    events,
    knowledge,
    reports,
    robots,
    simulations,
    stats,
    tasks,
    ws,
)

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["认证"])
api_router.include_router(audit.router, prefix="/audit", tags=["审计"])
api_router.include_router(ai.router, prefix="/ai", tags=["AI"])
api_router.include_router(agents.router, prefix="/agents", tags=["智能体"])
api_router.include_router(assistant.router, prefix="/assistant", tags=["对话助手"])
api_router.include_router(knowledge.router, prefix="/knowledge", tags=["知识智能体"])
api_router.include_router(events.router, prefix="/events", tags=["事件"])
api_router.include_router(tasks.router, prefix="/tasks", tags=["任务"])
api_router.include_router(devices.router, prefix="/devices", tags=["设备"])
api_router.include_router(robots.router, prefix="/robots", tags=["机器人"])
api_router.include_router(simulations.router, prefix="/simulations", tags=["仿真"])
api_router.include_router(reports.router, prefix="/reports", tags=["报表"])
api_router.include_router(stats.router, prefix="/stats", tags=["统计"])
api_router.include_router(ws.router, tags=["WebSocket"])
