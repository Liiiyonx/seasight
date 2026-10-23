"""漂浮垃圾检测器：``plastic_bag`` 目标类 + ``buoy`` 显式负类。

三种后端按可用性自动选定：

* ``onnx``        ``cv2.dnn.readNetFromONNX`` 加载 ``best.onnx``
                  —— **推荐**：零额外依赖，Pi 上比 .pt 快得多
* ``ncnn``        装了 ncnn 包时优先（速度最好）
* ``ultralytics`` 直接跑 ``best.pt``（开发机验证用，Pi 上偏慢）
* ``mock``        无模型时的合成检测（HSV/轮廓），仅供离线调试 UI 与状态机
* ``none``        **真机模式下没找到模型** —— 返回空结果并在状态里明示，
                  绝不合成假检测（把假框当成真检出是最危险的静默缺陷）

类别顺序由配置 ``vision.classes`` 决定，必须与训练时一致，否则输出类
索引会整体错位。这里只实现 YOLOv8 系列的 (1, 4+nc, N) 头解算 ——
现场只部署自己导出的模型，兼容面越窄越不容易翻车。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("oceanus.grasp.detector")


class DetectorError(RuntimeError):
    """模型加载失败或推理不可用。"""


@dataclass
class Detection:
    cls: str
    conf: float
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def grasp_point(self) -> tuple[float, float]:
        """抓取点：检测框下沿略上（袋口）。

        塑料袋形变时框会变，但「袋口在下沿」这一先验比几何中心稳，
        夹爪从袋口下探的滑脱率明显更低。
        """
        return ((self.x1 + self.x2) / 2.0, self.y2 - (self.y2 - self.y1) * 0.15)

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    def to_dict(self) -> dict[str, Any]:
        u, v = self.center
        gu, gv = self.grasp_point
        return {
            "cls": self.cls,
            "conf": round(float(self.conf), 3),
            "bbox": [round(self.x1, 1), round(self.y1, 1), round(self.x2, 1), round(self.y2, 1)],
            "center": [round(u, 1), round(v, 1)],
            "grasp_point": [round(gu, 1), round(gv, 1)],
        }


class Detector:
    """检测器封装（真机 + mock 两态，后端自动降级）。"""

    def __init__(self, cfg: dict[str, Any], mock: bool | None = None) -> None:
        vision = cfg.get("vision", {}) or {}
        # ★ 两个口径分开：classes 是「标注可选类别」，model_classes 是「在用模型
        #   实际输出的类别」。标注台要能选 buoy（负类样本），但部署的单类模型只输出
        #   plastic_bag —— 若两者共用一个字段，要么标注台少了 buoy，要么类别索引错位。
        self.classes: list[str] = list(
            vision.get("model_classes") or vision.get("classes")
            or ["plastic_bag", "buoy"]
        )
        self.target_class: str = str(vision.get("target_class", "plastic_bag"))
        self.input_size = int(vision.get("input_size", 320))
        self.conf_threshold = float(vision.get("conf_threshold", 0.6))
        self.iou_threshold = float(vision.get("iou_threshold", 0.45))
        model_dir = Path(str(vision.get("model_dir", "models/active")))
        if not model_dir.is_absolute():
            # 相对路径按 edge/grasp/ 解析，避免受启动进程的工作目录影响
            model_dir = Path(__file__).resolve().parent.parent / model_dir
        self.model_dir = model_dir
        self.mock = bool(mock if mock is not None else (cfg.get("web", {}) or {}).get("mock"))

        #: 免训练校准写入的调参（阈值 / ROI），load() 时叠加到配置之上
        self.tuning: dict[str, Any] = {}
        #: 归一化 ROI (x, y, w, h)；只保留中心落在其中的检测
        self.roi: tuple[float, float, float, float] | None = None

        self.backend = "mock" if self.mock else "unloaded"
        self.model_path: str | None = None
        self._net: Any = None
        self._ncnn: Any = None
        self._yolo: Any = None
        #: ★ cv2.dnn / ncnn 的 Net **不是线程安全的**。作业台有两处并发调用
        #:   ``infer()``：后台推理线程（供 MJPEG 叠加）与抓取闭环。共用同一个
        #:   Net 并发 forward() 会产出垃圾结果 —— 实测同一帧串行结果唯一，并发
        #:   时能吐出几十个 conf=1.0 的假 buoy，表现为「抓取时好时坏、要重试
        #:   多次甚至全失败」。这里把推理入口串行化。
        self._infer_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------
    def load(self) -> str:
        """选定并加载后端，返回后端名。"""
        # 先叠加免训练校准的调参（阈值 / ROI）—— 它优先于配置文件默认值
        self._apply_tuning()

        if self.mock:
            self.backend = "mock"
            return self.backend

        onnx = self.model_dir / "best.onnx"
        ncnn_param = self.model_dir / "best.ncnn.param"
        pt = self.model_dir / "best.pt"

        if ncnn_param.exists():
            if self._load_ncnn(ncnn_param):
                return self.backend
        if onnx.exists():
            if self._load_onnx(onnx):
                return self.backend
        if pt.exists() and self._load_ultralytics(pt):
            return self.backend

        # 真机模式下没有模型：明确报 "none"，不合成假检测
        self.backend = "none"
        self.model_path = None
        logger.warning("未在 %s 找到可用模型（best.onnx / best.ncnn / best.pt）", self.model_dir)
        return self.backend

    def _apply_tuning(self) -> None:
        """叠加免训练校准产物 tuning.json（阈值 / ROI）。"""
        path = self.model_dir / "tuning.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("tuning.json 不可读，忽略: %s", exc)
            return
        if not isinstance(data, dict):
            return
        self.tuning = data
        if "conf_threshold" in data:
            self.conf_threshold = float(data["conf_threshold"])
        if "iou_threshold" in data:
            self.iou_threshold = float(data["iou_threshold"])
        roi = data.get("roi")
        # ★ 键缺失或为 null 时必须把旧 ROI 清掉：Detector 是长驻对象，_apply_tuning
        #   每次 load() 都会叠加一次。若写成"有则设、无则不动"，作业台的「取消 ROI」
        #   就会看起来成功、实际仍旧生效 —— 这种"按钮点了不生效"的坑最难查。
        self.roi = (tuple(float(x) for x in roi)
                    if isinstance(roi, (list, tuple)) and len(roi) == 4 else None)

    def _in_roi(self, det: "Detection", frame: Any) -> bool:
        if self.roi is None:
            return True
        h, w = frame.shape[:2]
        rx, ry, rw, rh = self.roi
        u, v = det.center
        return (rx * w) <= u <= (rx + rw) * w and (ry * h) <= v <= (ry + rh) * h

    def _load_onnx(self, path: Path) -> bool:
        try:
            import cv2
        except ImportError:
            return False
        try:
            self._net = cv2.dnn.readNetFromONNX(str(path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ONNX 加载失败 %s: %s", path, exc)
            return False
        self.backend = "onnx"
        self.model_path = str(path)
        return True

    def _load_ncnn(self, param: Path) -> bool:
        try:
            import ncnn  # noqa: F401
        except ImportError:
            return False
        bin_path = param.with_suffix(".bin")
        if not bin_path.exists():
            return False
        try:
            net = ncnn.Net()
            net.opt.num_threads = 4
            net.load_param(str(param))
            net.load_model(str(bin_path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("NCNN 加载失败 %s: %s", param, exc)
            return False
        self._ncnn = net
        self.backend = "ncnn"
        self.model_path = str(param)
        return True

    def _load_ultralytics(self, path: Path) -> bool:
        try:
            from ultralytics import YOLO
        except ImportError:
            return False
        try:
            self._yolo = YOLO(str(path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("YOLO 加载失败 %s: %s", path, exc)
            return False
        self.backend = "ultralytics"
        self.model_path = str(path)
        return True

    # ------------------------------------------------------------------
    # 推理
    # ------------------------------------------------------------------
    def infer(self, frame: Any) -> list[Detection]:
        """推理入口（**线程安全**）。

        ★ 加锁串行：见 ``_infer_lock`` 的说明。每帧多等一次锁的开销远小于
        推理本身（几十~几百毫秒），换来的是结果不被并发污染。
        """
        with self._infer_lock:
            return self._infer(frame)

    def _infer(self, frame: Any) -> list[Detection]:
        if frame is None:
            return []
        if self.backend == "mock" or self.mock:
            return self._infer_mock(frame)
        if self.backend == "onnx":
            return self._infer_onnx(frame)
        if self.backend == "ncnn":
            return self._infer_ncnn(frame)
        if self.backend == "ultralytics":
            return self._infer_ultralytics(frame)
        return []      # backend == "none"：没模型就没检测，绝不编造

    def targets(self, frame: Any) -> list[Detection]:
        """仅返回进入抓取候选的检测（目标类 + 置信度门控 + ROI）。

        这是抗浮标三层防线里的**推理层**：浮标即使被检出，类名对不上
        就进不了候选，机械臂永远不会朝它伸手。ROI 用于屏蔽画面边缘的
        水面反光带（免训练校准写入）。
        """
        return [
            d for d in self.infer(frame)
            if d.cls == self.target_class
            and d.conf >= self.conf_threshold
            and self._in_roi(d, frame)
        ]

    def _blob(self, frame: Any) -> tuple[Any, float]:
        import cv2

        h, w = frame.shape[:2]
        # letterbox 到 input_size 的方图，保持长宽比（与 YOLO 训练口径一致）。
        # 注意这里贴的是左上角（不居中），所以还原时只需除以缩放比。
        scale = min(self.input_size / w, self.input_size / h)
        nw, nh = int(round(w * scale)), int(round(h * scale))
        resized = cv2.resize(frame, (nw, nh))
        canvas = cv2.copyMakeBorder(
            resized, 0, self.input_size - nh, 0, self.input_size - nw,
            cv2.BORDER_CONSTANT, value=(114, 114, 114),
        )
        blob = cv2.dnn.blobFromImage(canvas, 1 / 255.0, (self.input_size, self.input_size),
                                     swapRB=True, crop=False)
        return blob, scale

    def _decode(self, out: Any, frame: Any, scale: float) -> list[Detection]:
        """YOLOv8 (1, 4+nc, N) 头解算 + NMS。"""
        import cv2
        import numpy as np

        pred = np.squeeze(out, axis=0)           # (4+nc, N)
        if pred.shape[0] < pred.shape[1]:
            pred = pred.T                        # (N, 4+nc)
        boxes_xywh = pred[:, :4]
        scores_all = pred[:, 4:]
        cls_ids = scores_all.argmax(axis=1)
        confs = scores_all.max(axis=1)

        keep = confs >= self.conf_threshold
        boxes_xywh, cls_ids, confs = boxes_xywh[keep], cls_ids[keep], confs[keep]
        if len(boxes_xywh) == 0:
            return []

        boxes_xyxy = np.empty_like(boxes_xywh)
        boxes_xyxy[:, 0] = boxes_xywh[:, 0] - boxes_xywh[:, 2] / 2
        boxes_xyxy[:, 1] = boxes_xywh[:, 1] - boxes_xywh[:, 3] / 2
        boxes_xyxy[:, 2] = boxes_xywh[:, 0] + boxes_xywh[:, 2] / 2
        boxes_xyxy[:, 3] = boxes_xywh[:, 1] + boxes_xywh[:, 3] / 2

        # 从 letterbox 坐标还原到原图
        boxes_xyxy /= scale
        h, w = frame.shape[:2]
        boxes_xyxy[:, [0, 2]] = boxes_xyxy[:, [0, 2]].clip(0, w - 1)
        boxes_xyxy[:, [1, 3]] = boxes_xyxy[:, [1, 3]].clip(0, h - 1)

        # ★ NMS 必须**按类各做一次**，不能对所有框做一次类别无关的 NMS。
        #   ``cv2.dnn.NMSBoxes`` 是类别无关的：同一目标被同时预测成两个类时，
        #   得分高的那个类会把另一个类**整框压掉**。2 类模型（plastic_bag +
        #   buoy 显式负类）上实测过：泳池帧里同一个袋子被同时预测成
        #   plastic_bag 0.96 与 buoy 0.97，一次类别无关的 NMS 把袋子框删掉 ——
        #   留出集召回从 0.993 掉到 **0.776**（部署链路上少检 22% 的目标）。
        #   ultralytics 默认 agnostic_nms=False（按类 NMS），这里与它对齐。
        keep_idx: list[int] = []
        for cls_id in np.unique(cls_ids):
            m = cls_ids == cls_id
            sub = np.array(cv2.dnn.NMSBoxes(boxes_xyxy[m].tolist(), confs[m].tolist(),
                                            self.conf_threshold, self.iou_threshold)).flatten()
            keep_idx.extend(int(i) for i in np.where(m)[0][sub])

        dets: list[Detection] = []
        for i in keep_idx:
            ci = int(cls_ids[i])
            name = self.classes[ci] if ci < len(self.classes) else f"cls{ci}"
            x1, y1, x2, y2 = boxes_xyxy[i]
            dets.append(Detection(name, float(confs[i]), float(x1), float(y1), float(x2), float(y2)))
        return dets

    def _infer_onnx(self, frame: Any) -> list[Detection]:
        blob, scale = self._blob(frame)
        self._net.setInput(blob)
        out = self._net.forward()
        return self._decode(out, frame, scale)

    def _infer_ncnn(self, frame: Any) -> list[Detection]:
        import numpy as np

        blob, scale = self._blob(frame)
        arr = blob[0].transpose(1, 2, 0)          # (H, W, C)
        mat = self._ncnn.Mat(arr).clone()
        ex = self._ncnn.create_extractor()
        ex.input("images", mat)
        _ret, out = ex.extract("output0")
        return self._decode(np.expand_dims(np.array(out), 0), frame, scale)

    def _infer_ultralytics(self, frame: Any) -> list[Detection]:
        res = self._yolo.predict(frame, imgsz=self.input_size, conf=self.conf_threshold,
                                 iou=self.iou_threshold, verbose=False)[0]
        dets: list[Detection] = []
        names = getattr(res, "names", {}) or {}
        for box in res.boxes:
            ci = int(box.cls[0])
            name = names.get(ci, self.classes[ci] if ci < len(self.classes) else f"cls{ci}")
            x1, y1, x2, y2 = (float(v) for v in box.xyxy[0])
            dets.append(Detection(str(name), float(box.conf[0]), x1, y1, x2, y2))
        return dets

    def _infer_mock(self, frame: Any) -> list[Detection]:
        """合成检测：用 HSV 把「低饱和浅色团块」判成袋、「高饱和彩色团块」判成浮标。

        刻意沿用真实场景的颜色先验（袋低饱和、浮标高饱和），
        这样离线调试时抗浮标三层防线里的 HSV 决策层也能被真实走到。
        """
        try:
            import cv2
            import numpy as np
        except ImportError:
            return []
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        h_ch, s_ch, v_ch = cv2.split(hsv)
        dets: list[Detection] = []

        # 浮标：高饱和 + 中高亮度
        sat_mask = cv2.inRange(s_ch, 150, 255) & cv2.inRange(v_ch, 110, 255)
        sat_mask = cv2.morphologyEx(sat_mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        for c in cv2.findContours(sat_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
            if cv2.contourArea(c) < 400:
                continue
            x, y, w, h = cv2.boundingRect(c)
            dets.append(Detection("buoy", 0.85, x, y, x + w, y + h))

        # 塑料袋：低饱和 + 高亮度 + 横向扁平（团块）
        bag_mask = cv2.inRange(s_ch, 0, 70) & cv2.inRange(v_ch, 165, 255)
        bag_mask = cv2.morphologyEx(bag_mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        for c in cv2.findContours(bag_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
            area = cv2.contourArea(c)
            if area < 800:
                continue
            x, y, w, h = cv2.boundingRect(c)
            if h == 0 or w / h < 1.2:
                continue
            dets.append(Detection("plastic_bag", 0.92, x, y, x + w, y + h))

        return dets

    # ------------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "model": self.model_path,
            "classes": self.classes,
            "target_class": self.target_class,
            "conf_threshold": self.conf_threshold,
            "iou_threshold": self.iou_threshold,
            "input_size": self.input_size,
            "roi": list(self.roi) if self.roi else None,
            "tuned": bool(self.tuning),
            "ready": self.backend not in ("unloaded", "none"),
        }
