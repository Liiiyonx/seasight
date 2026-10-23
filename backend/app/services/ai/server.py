"""AI 推理服务：把 YOLO 模型封装成平台可调用的 HTTP 服务。

为什么独立服务而不是库函数：
- 模型可能更新、可能换框架（ONNX ↔ TensorRT ↔ RKNN）
- 独立服务让算法组能单独发版，不影响平台

接口：
    POST /infer/detect   传入图片，返回检测结果
    GET  /infer/health   健康检查与当前模型版本
    POST /infer/reload   热加载新模型
"""

from __future__ import annotations

import io
import os
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

# 模型版本（由环境变量注入，事件表会记录此版本便于追溯）
MODEL_VERSION = os.getenv("AI_MODEL_VERSION", "det_v0.1.0")
MODEL_PATH = os.getenv("AI_MODEL_PATH", "/ml/models/best.onnx")
CONF_THRESHOLD = float(os.getenv("AI_CONF_THRESHOLD", "0.25"))
IOU_THRESHOLD = float(os.getenv("AI_IOU_THRESHOLD", "0.45"))
INPUT_SIZE = int(os.getenv("AI_INPUT_SIZE", "640"))

# 类别定义（顺序即模型输出索引，必须与训练时 datasets yaml 一致）
CLASS_NAMES = ["foam", "plastic", "fishing_gear", "other"]
CLASS_LABELS = {
    "foam": "泡沫类",
    "plastic": "塑胶类",
    "fishing_gear": "渔具类",
    "other": "其他",
}


class Detector:
    """检测器封装。

    支持两种后端（自动探测）：
    - onnxruntime：跨平台，Jetson / RK3588 / x86 都可用
    - 纯 stub 模式：模型文件缺失时返回模拟结果（保证平台可独立调试）
    """

    def __init__(self) -> None:
        self.session: Any = None
        self.backend = "stub"
        self.model_version = MODEL_VERSION
        self.loaded_at: datetime | None = None

    def load(self, model_path: str) -> bool:
        """加载模型。失败则降级到 stub 模式（不阻断服务启动）。"""
        if not os.path.exists(model_path):
            logger.warning(
                f"[AI] 模型文件不存在：{model_path} —— 进入 stub 模式（返回模拟结果）"
            )
            self.backend = "stub"
            self.loaded_at = datetime.now()
            return False

        try:
            import onnxruntime as ort

            providers = ort.get_available_providers()
            # 优先 GPU，回落 CPU
            preferred = [
                p for p in ("TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider")
                if p in providers
            ]
            self.session = ort.InferenceSession(model_path, providers=preferred or None)
            self.backend = f"onnxruntime:{preferred[0] if preferred else 'CPU'}"
            self.loaded_at = datetime.now()
            logger.info(f"[AI] 模型已加载：{model_path} 后端={self.backend}")
            return True
        except Exception as exc:   # noqa: BLE001
            logger.error(f"[AI] 模型加载失败：{exc} —— 降级 stub 模式")
            self.backend = "stub"
            self.loaded_at = datetime.now()
            return False

    def infer(self, image_bytes: bytes) -> list[dict[str, Any]]:
        """执行推理。

        stub 模式下返回确定性模拟结果（基于图片字节数生成，
        保证同一图重复调用结果一致，便于测试）。
        """
        if self.backend == "stub" or self.session is None:
            return self._stub_infer(image_bytes)

        import numpy as np
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        original_w, original_h = image.size

        # ★ letterbox：等比缩放 + 灰边填充，而不是直接 resize。
        #
        # 为什么不能直接 resize ——
        # 直接拉伸会让宽高比失真（1080p 是 16:9，640×640 是 1:1）。
        # 模型在训练时见到的是 letterbox 后的图（Ultralytics 默认），
        # 推理时不照做，框的位置与形状系统性偏移，
        # 越靠画面边缘偏移越大。
        #
        # 为什么要记录 pad ——
        # 后处理把框从 640×640 坐标系还原回原图时，必须先减掉 padding
        # 再除以缩放比。少了这一步，所有框都会朝左上角整体平移。
        resized, scale, pad_x, pad_y = _letterbox(image, INPUT_SIZE)

        arr = np.asarray(resized, dtype=np.float32) / 255.0
        arr = np.transpose(arr, (2, 0, 1))[None, ...]   # NCHW

        input_name = self.session.get_inputs()[0].name
        outputs = self.session.run(None, {input_name: arr})

        return self._postprocess(
            outputs,
            original_w,
            original_h,
            CONF_THRESHOLD,
            IOU_THRESHOLD,
            scale=scale,
            pad_x=pad_x,
            pad_y=pad_y,
        )

    def _postprocess(
        self,
        outputs: list[Any],
        orig_w: int,
        orig_h: int,
        conf_thres: float,
        iou_thres: float,
        scale: float = 1.0,
        pad_x: float = 0.0,
        pad_y: float = 0.0,
    ) -> list[dict[str, Any]]:
        """后处理：把模型输出解析成契约格式的检测列表。

        ★ 必须同时支持两种导出格式 —— 这是踩过的坑：

        | 导出方式 | 输出形状 | 含义 |
        | --- | --- | --- |
        | `nms=False`（本项目训练配置选它） | `(1, 4+nc, anchors)` | cx,cy,w,h + 类别分数 |
        | `nms=True`（EfficientNMS 烘进图） | `(1, N, 6)` | x1,y1,x2,y2,score,class_id |

        早期实现只认第一种，第二种会直接抛 ValueError（拿 6 个元素当
        4+nc 拆，类别分数取空）。因为一直没有真实模型（走的 stub），
        这个缺陷长期潜伏 —— 一旦真模型挂上去，推理接口当场 500。

        为什么不干脆只用一种：`nms=False` 便于部署后调 IoU 阈值
        （不同海域垃圾密度差异大），是我们的默认；但队友若为了
        省一次 NMS 而改成 `nms=True`，这里也必须能接住。
        """
        import numpy as np

        out = np.asarray(outputs[0])
        if out.ndim == 3:
            out = out[0]          # 去掉 batch 维 → (N, C) 或 (C, N)

        # ---- 格式判别：EfficientNMS 输出固定 6 列且末列为整数类别 ----
        if out.ndim == 2 and out.shape[-1] == 6:
            return self._parse_nms_baked(
                out, orig_w, orig_h, conf_thres, scale, pad_x, pad_y
            )

        # ---- 原始输出 (4+nc, anchors) → 转置成 (anchors, 4+nc) ----
        preds = out
        if preds.shape[0] < preds.shape[1]:
            preds = preds.T

        boxes: list[list[float]] = []
        scores: list[float] = []
        class_ids: list[int] = []

        for row in preds:
            cls_scores = row[4:]
            if cls_scores.size == 0:
                continue
            class_id = int(np.argmax(cls_scores))
            confidence = float(cls_scores[class_id])
            if confidence < conf_thres:
                continue
            cx, cy, w, h = row[0], row[1], row[2], row[3]
            boxes.append([
                float((cx - w / 2 - pad_x) / scale),
                float((cy - h / 2 - pad_y) / scale),
                float((cx + w / 2 - pad_x) / scale),
                float((cy + h / 2 - pad_y) / scale),
            ])
            scores.append(confidence)
            class_ids.append(class_id)

        # NMS（nms=False 导出时才需要自己算）
        keep = _nms(boxes, scores, iou_thres)
        return _build_results(boxes, scores, class_ids, keep, orig_w, orig_h)

    def _parse_nms_baked(
        self,
        preds: Any,
        orig_w: int,
        orig_h: int,
        conf_thres: float,
        scale: float,
        pad_x: float,
        pad_y: float,
    ) -> list[dict[str, Any]]:
        """解析 EfficientNMS 已烘进图的输出：[x1,y1,x2,y2,score,class_id]。"""
        import numpy as np

        boxes: list[list[float]] = []
        scores: list[float] = []
        class_ids: list[int] = []

        for row in preds:
            confidence = float(row[4])
            if confidence < conf_thres:
                continue
            x1, y1, x2, y2 = (float(row[0]), float(row[1]), float(row[2]), float(row[3]))
            # 已烘 NMS 的坐标同样基于 letterbox 后的 640×640，需还原
            boxes.append([
                (x1 - pad_x) / scale,
                (y1 - pad_y) / scale,
                (x2 - pad_x) / scale,
                (y2 - pad_y) / scale,
            ])
            scores.append(confidence)
            class_ids.append(int(row[5]))

        # 图里已经做过 NMS，这里不再重复压制
        keep = list(range(len(boxes)))
        _ = np  # 保持 numpy 依赖显式，便于后续扩展
        return _build_results(boxes, scores, class_ids, keep, orig_w, orig_h)

    def _stub_infer(self, image_bytes: bytes) -> list[dict[str, Any]]:
        """模拟推理（模型未就绪时保证平台可调试）。

        用字节哈希生成确定性结果——同一张图结果一致，便于写测试断言。
        """
        import hashlib

        digest = int(hashlib.md5(image_bytes).hexdigest()[:8], 16)
        count = digest % 4 + 1                      # 1~4 个目标
        cls = CLASS_NAMES[digest % len(CLASS_NAMES)]

        results = []
        for i in range(count):
            seed = (digest >> (i * 4)) % 1000
            x1 = 100 + seed % 400
            y1 = 80 + (seed * 3) % 300
            results.append({
                "class": cls,
                "confidence": round(0.70 + (seed % 25) / 100, 4),
                "bbox": [x1, y1, x1 + 120, y1 + 90],
                "note": "stub 模式模拟结果（模型未加载）",
            })
        return results


def _letterbox(image: Any, size: int, pad_value: int = 114) -> tuple[Any, float, float, float]:
    """等比缩放并灰边填充到 size×size。

    返回 `(填充后的图, scale, pad_x, pad_y)`，其中 scale 是缩放比
    （resized 边长 / 原边长），pad 是左右/上下各加的边宽。

    ★ 为什么 pad 值用 114 灰：Ultralytics 训练时的默认填充色就是 114，
      推理端必须与训练端一致。换成纯黑会让模型在图像边界处
      看到训练时从未见过的分布，边缘目标的置信度会莫名偏低。
    """
    from PIL import Image

    w, h = image.size
    if w == 0 or h == 0:
        return image.resize((size, size)), 1.0, 0.0, 0.0

    scale = min(size / w, size / h)
    new_w, new_h = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    resized = image.resize((new_w, new_h), Image.BILINEAR)

    canvas = Image.new("RGB", (size, size), (pad_value, pad_value, pad_value))
    pad_x = (size - new_w) // 2
    pad_y = (size - new_h) // 2
    canvas.paste(resized, (pad_x, pad_y))

    # 用实际用到的整数形变比，而不是浮点 scale ——
    # resize 时 round 过，用浮点值还原会累积零点几像素的系统偏差
    return canvas, (new_w / w), float(pad_x), float(pad_y)


def _build_results(
    boxes: list[list[float]],
    scores: list[float],
    class_ids: list[int],
    keep: list[int],
    orig_w: int,
    orig_h: int,
) -> list[dict[str, Any]]:
    """把框/分数/类别组装成契约格式，并裁剪到图像范围内。

    ★ 裁剪是必须的：letterbox 还原后，边缘目标的框可能落到负数或
      超出原图尺寸（padding 区域的预测）。契约（mqtt-topics.md）
      要求 bbox 是「设备原始分辨率下的像素坐标，原点左上」，
      前端按 bbox/分辨率 换算百分比定位 —— 越界的值会让框跑到
      容器外面去。所以在这里夹住再做整数化。
    """
    results: list[dict[str, Any]] = []
    for idx in keep:
        x1, y1, x2, y2 = boxes[idx]
        x1 = max(0.0, min(float(orig_w), x1))
        y1 = max(0.0, min(float(orig_h), y1))
        x2 = max(0.0, min(float(orig_w), x2))
        y2 = max(0.0, min(float(orig_h), y2))
        # 裁剪后可能退化成一个点，直接丢弃 —— 没有面积的框不是有效目标
        if x2 - x1 < 1 or y2 - y1 < 1:
            continue
        cid = class_ids[idx]
        results.append({
            "class": CLASS_NAMES[cid] if 0 <= cid < len(CLASS_NAMES) else "other",
            "confidence": round(float(scores[idx]), 4),
            "bbox": [int(x1), int(y1), int(x2), int(y2)],
        })
    return results


def _nms(boxes: list[list[float]], scores: list[float], iou_thres: float) -> list[int]:
    """非极大值抑制。"""
    if not boxes:
        return []

    import numpy as np

    boxes_arr = np.array(boxes, dtype=np.float32)
    scores_arr = np.array(scores, dtype=np.float32)

    x1, y1, x2, y2 = boxes_arr[:, 0], boxes_arr[:, 1], boxes_arr[:, 2], boxes_arr[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores_arr.argsort()[::-1]

    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)
        remaining = np.where(iou <= iou_thres)[0]
        order = order[remaining + 1]
    return keep


# ----------------------------------------------------------------------
# FastAPI 应用
# ----------------------------------------------------------------------
detector = Detector()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """启动时加载模型，关闭时释放。

    用 lifespan 而不是已废弃的 @app.on_event("startup")：
    on_event 在 FastAPI 新版本会持续告警，且多个 handler 的执行顺序
    没有保证 —— lifespan 里是普通顺序代码，加载逻辑一目了然。
    """
    detector.load(MODEL_PATH)
    logger.info(f"[AI] 服务启动，后端={detector.backend} 模型版本={detector.model_version}")
    try:
        yield
    finally:
        logger.info("[AI] 服务关闭")


app = FastAPI(
    title="探海灵眸 AI 推理服务",
    version="0.1.0",
    description="海漂垃圾检测模型推理服务（独立进程，算法组可单独发版）",
    lifespan=lifespan,
)


@app.get("/infer/health", summary="健康检查")
async def health() -> dict:
    return {
        "status": "ok",
        "backend": detector.backend,
        "model_version": detector.model_version,
        "model_path": MODEL_PATH,
        "loaded_at": detector.loaded_at.isoformat() if detector.loaded_at else None,
        "input_size": INPUT_SIZE,
        "classes": CLASS_NAMES,
    }


@app.post("/infer/detect", summary="目标检测")
async def detect(file: UploadFile = File(...)) -> dict:
    """传入图片，返回检测结果。

    返回格式与边缘端事件报文中的 detections 字段一致，
    便于边缘盒直接组装上报。
    """
    start = time.perf_counter()
    image_bytes = await file.read()

    try:
        detections = detector.infer(image_bytes)
    except Exception as exc:   # noqa: BLE001
        logger.exception(f"[AI] 推理失败：{exc}")
        return JSONResponse(
            status_code=200,
            content={
                "code": 5002,
                "message": f"推理失败：{exc}",
                "data": None,
            },
        )

    elapsed_ms = (time.perf_counter() - start) * 1000

    # 聚合结果（与边缘端 aggregate 结构一致）
    main_class = detections[0]["class"] if detections else None
    aggregate = {
        "main_class": main_class,
        "count": len(detections),
        "max_confidence": max((d["confidence"] for d in detections), default=0.0),
    }

    return {
        "code": 0,
        "message": "ok",
        "data": {
            "detections": detections,
            "aggregate": aggregate,
            "model_version": detector.model_version,
            "backend": detector.backend,
            "elapsed_ms": round(elapsed_ms, 2),
        },
    }


@app.post("/infer/reload", summary="热加载模型")
async def reload_model(model_path: str | None = Form(default=None)) -> dict:
    """热加载新模型（不重启服务）。"""
    target = model_path or MODEL_PATH
    success = detector.load(target)
    return {
        "code": 0 if success else 5001,
        "message": "模型已重载" if success else "模型加载失败（已降级 stub）",
        "data": {
            "backend": detector.backend,
            "model_version": detector.model_version,
            "model_path": target,
        },
    }
