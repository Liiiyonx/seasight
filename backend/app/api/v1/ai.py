"""AI 图片分析接口 —— 上传一张海漂垃圾图片，用 OpenCV 检测器分析。

复用 `edge/detector` 的 CvDetector（零数据依赖的识别方案），
输出契约与事件上报一致（`{class, confidence, bbox}`），
演示/答辩时可现场上传图片看到真实检测框。

为什么不是调 YOLO：数据集还没攒够，走 OpenCV 传统视觉（背景建模 +
颜色通道 + 轮廓规则），识别可以不准但绝不是假的。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from fastapi import APIRouter, File, UploadFile

from app.core.exceptions import ApiResponse, ErrorCode

router = APIRouter()

# edge/detector 在 seahawk/ 根下，而 backend 运行时 sys.path 是 backend/。
# 这里把 seahawk 根加入 sys.path，才能 `from edge.detector.detector import CvDetector`。
_SEAHAWK_ROOT = Path(__file__).resolve().parents[4]
if str(_SEAHAWK_ROOT) not in sys.path:
    sys.path.insert(0, str(_SEAHAWK_ROOT))


# ----------------------------------------------------------------------
# 智能体（Agent）目录 —— 把平台的核心能力包装成「智能体」展示。
# 每个智能体对应一个真实服务模块，不是空壳：
#   识别 → edge/detector + 时序校验
#   调度 → services/dispatch
#   分析 → services/report + stats
#   巡检 → devices 心跳 + robots 状态
#   告警 → services/notify + ws 推送
#   审计 → services/audit
# ----------------------------------------------------------------------
AGENTS: list[dict] = [
    {
        "id": "vision",
        "name": "识别智能体",
        "codename": "灵眸·视",
        "glyph": "视",
        "role": "海漂垃圾识别",
        "status": "running",
        "capabilities": ["OpenCV 检测", "时序校验", "四类别分类", "反光抑制"],
        "description": "把摄像头画面变成结构化事件：泡沫、塑胶、渔具、其他四类识别；合成链路时序误报抑制率约 76%（被过滤检测数 / 输入检测数），不代表识别精度。",
    },
    {
        "id": "dispatch",
        "name": "调度智能体",
        "codename": "灵眸·策",
        "glyph": "策",
        "role": "派单调度",
        "status": "running",
        "capabilities": ["五步筛选", "就近派单", "类别加权", "ACK 超时换车"],
        "description": "事件到工单的决策中枢：在线/电量/仓容过滤 → KNN 就近 → 同类别顺路加权 → 下发 → 超时换车重派。",
    },
    {
        "id": "analyst",
        "name": "数据分析智能体",
        "codename": "灵眸·析",
        "glyph": "析",
        "role": "治理数据分析",
        "status": "running",
        "capabilities": ["每日聚合", "热力图", "量化报表", "乡镇归属"],
        "description": "把明细数据沉淀成日报表：事件/工单按乡镇与类别聚合，热力图呈现空间分布，报表真实会涨。",
    },
    {
        "id": "patrol",
        "name": "巡检智能体",
        "codename": "灵眸·巡",
        "glyph": "巡",
        "role": "设备态势感知",
        "status": "running",
        "capabilities": ["心跳监测", "在线状态", "电量/仓容", "断网补传"],
        "description": "盯着每一台设备：心跳、电量、三仓占用，离线即标记，保障「感知」这第一环不掉线。",
    },
    {
        "id": "alert",
        "name": "告警智能体",
        "codename": "灵眸·警",
        "glyph": "警",
        "role": "告警外发",
        "status": "running",
        "capabilities": ["企业微信推送", "站内通知", "大屏滚动", "@到人"],
        "description": "高优先级事件不只在大屏滚：异步推送到企业微信机器人、站内通知铃铛，告警真正到人。",
    },
    {
        "id": "audit",
        "name": "审计智能体",
        "codename": "灵眸·印",
        "glyph": "印",
        "role": "操作审计",
        "status": "running",
        "capabilities": ["操作留痕", "谁改了什么", "管理员回溯"],
        "description": "谁在何时对什么做了什么，全部落审计日志，政务交付的可追溯性闭环。",
    },
]


@router.get("/agents", summary="智能体列表")
async def list_agents():
    """返回平台全部智能体的定义与真实运行时状态（前端智能体页展示）。

    能力目录与运行实例是两个概念：这些条目描述平台已实现的能力，
    `status` 则来自进程内 Agent Runtime 的真实快照。无活跃运行时
    显示 idle，不能用硬编码 running 把「有代码」冒充「正在执行」。
    """
    try:
        from app.api.v1.agents import get_agent_runtime

        runtime_status = get_agent_runtime().status()
        status = runtime_status.state
    except Exception:  # noqa: BLE001
        status = "unavailable"

    return ApiResponse.ok([{**agent, "status": status} for agent in AGENTS])


@router.post("/analyze-image", summary="图片分析（OpenCV 检测）")
async def analyze_image(file: UploadFile = File(...)):
    """上传图片 → OpenCV 检测 → 返回检测结果（类别/置信度/框）。

    图片解码失败、无检测框都正常返回（不抛异常），前端据此展示。

    依赖说明（线上踩过的坑）：本接口需要 cv2 + numpy，以及仓库根的 edge/ 包。
    两者都不在 backend 镜像的默认构建范围内 —— 依赖装在
    backend/requirements.txt，edge/ 由 docker-compose 只读挂载到容器内的 /edge。
    缺任一项都会 ImportError。这里显式接住并返回可读的业务错误，
    而不是让异常穿出去变成「服务器内部错误」——
    否则线上只会看到一个 500 和一个 trace_id，完全不知道是缺件。
    """
    try:
        from edge.detector.detector import CLASS_NAMES, CvDetector

        import cv2
        import numpy as np
    except ImportError as exc:  # noqa: BLE001
        return ApiResponse.fail(
            code=ErrorCode.AI_SERVICE_UNAVAILABLE,
            message=(
                f"图片检测组件不可用（{exc}）：请确认镜像已安装 opencv/numpy，"
                "且 edge/ 已挂载到容器"
            ),
        )

    data = await file.read()
    if not data:
        return ApiResponse.fail(code=4001, message="上传内容为空")

    nparr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        return ApiResponse.fail(code=4001, message="无法解析图片，请上传 jpg/png 格式")

    # 检测是 CPU 密集 + 同步，放线程池执行，避免阻塞事件循环
    # （与 services/assistant/tools.py 的 image.analyze 保持同一处理方式）
    detector = CvDetector()
    detections = await asyncio.to_thread(detector.detect, img)

    return ApiResponse.ok(
        {
            "detections": detections,
            "count": len(detections),
            "width": int(img.shape[1]),
            "height": int(img.shape[0]),
            "classes": CLASS_NAMES,
            "engine": "opencv",
        }
    )
