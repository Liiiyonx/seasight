"""OpenCV 海漂垃圾检测器 —— 不依赖训练数据的识别方案。

为什么有这个模块
----------------
原本的识别路线是 YOLO11s + ONNX，但它的前置依赖是**数据集**：
`ml/configs/seasight.yaml` 的 stats 至今是 0（一张图都没采）。
采集 → 标注 → 训练 → 导出 → 边缘部署，整条链 3~4 周且受天气与出海排期
支配，任何一环卡住全线停摆。

而 OpenCV 传统视觉方案的**数据依赖是 0**：不需要一张标注图，
今天写完明天就能在真实画面上跑出真实检测框。

它靠什么把"不准"这件事兜住
---------------------------
**下游的时序校验器**（`edge/simulator/simulator.py` 的 `TemporalValidator`）。
设计文档 §2.3 写得很清楚：原始检测 5772 条 → 确认事件 52 条，抑制率 42%。
也就是说，架构上**本来就是按"检测器会误报"设计的** —— 本模块只是把
一个高质量检测器换成一个低质量但零成本的检测器，误报由同一道闸压住。

真要换回 YOLO 怎么办
--------------------
本模块的输出契约与 `backend/app/services/ai/server.py` 的 detections
**完全一致**（`{class, confidence, bbox:[x1,y1,x2,y2]}`），
下游（时序校验 → MQTT 上报 → 派单 → 大屏）一行都不用改。
换模型只要换掉这一个类的 `detect()`。

输出契约
--------
    [{"class": "foam", "confidence": 0.72, "bbox": [x1, y1, x2, y2]}, ...]

`bbox` 是**设备原始分辨率下的像素坐标、原点左上**，与 mqtt-topics.md 一致。
"""

from __future__ import annotations

import math
from typing import Any

import cv2
import numpy as np

# 类别定义 —— 顺序即类别索引，必须与以下三处逐项一致：
#   1. ml/configs/seasight.yaml           的 names
#   2. backend/app/services/ai/server.py  的 CLASS_NAMES
#   3. docs/software-design.md            的类别定义
# 第四处定义很容易与前四处漂移，所以 tests 里有断言把这几处钉在一起。
CLASS_NAMES = ["foam", "plastic", "fishing_gear", "other"]


# 参数默认值 —— ★ 必须与 edge/config.yaml 的 detector 段逐项一致。
# 项目踩过的坑（见 docs/decisions.md）：默认值散落在代码里，
# 而 config.yaml 里改了却因为实例化时漏传不生效 —— 且不报错。
# 所以这里每一个默认值都在测试里与 config.yaml 对账。
DEFAULT_CONFIG: dict[str, Any] = {
    "background": {
        "history": 300,
        "dist2_threshold": 300.0,
        "detect_shadows": True,
    },
    "color": {
        "enabled": True,
        "v_min": 150,
        "s_max": 70,
        # 暗色通道：废旧渔网/湿缆绳比海面暗得多，而 KNN 背景建模会把
        # "比背景暗"的东西标成**阴影(127)**，在阈值化时被剔掉 ——
        # 实测暗色细长条的检测框因此整帧为空。所以必须单独一路抓暗目标。
        "dark_enabled": True,
        "dark_v_max": 70,
    },
    "morph_open": 3,
    "morph_close": 7,
    "min_area": 40,
    "max_area": 40000,
    "min_solidity": 0.35,
    "max_aspect": 8.0,
    "classifier": {
        "fishing_gear": {"aspect_min": 2.5, "solidity_max": 0.80, "v_max": 170},
        "foam": {"s_max": 70, "v_min": 150, "circularity_min": 0.20},
        "plastic": {"s_min": 70},
    },
    "confidence": {
        "base": 0.55,
        "span": 0.40,
        "min": 0.30,
        "max": 0.95,
        "glare": {
            "v_threshold": 245,
            "area_max": 400,
            "circularity_max": 0.55,
            "penalty": 0.25,
        },
        "size": {
            "small_factor": 0.4,
            "small_times": 2,
            "large_factor": 0.5,
            "large_ratio": 0.5,
        },
    },
}


def _get(config: dict[str, Any] | None, section: str, key: str) -> Any:
    """从配置取值，缺失或配置未给该段时回落到默认。

    为什么逐键回退而不是整体 `config or DEFAULT_CONFIG`：
    真实边缘盒上的 config.yaml 很可能是旧版（少几个新加的键），
    整体回退会让运维在 yaml 里改的参数**静默失效** ——
    这正是本项目反复出现的缺陷形状。
    """
    if config:
        sec = config.get(section)
        if isinstance(sec, dict) and key in sec:
            return sec[key]
    return DEFAULT_CONFIG[section][key]


def _top(config: dict[str, Any] | None, key: str) -> Any:
    """取 detector 段下的顶层键（不在子段里的那些）。"""
    if config and key in config:
        return config[key]
    return DEFAULT_CONFIG[key]


def _clip01(value: float) -> float:
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else float(value))


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


class CvDetector:
    """OpenCV 海漂垃圾检测器。

    用法（真实边缘盒）：

        det = CvDetector(config["detector"])
        for frame in stream:
            for d in det.detect(frame):
                ...  # 送 TemporalValidator

    两路候选来源（取并集）：
      1. **背景减除** —— 抓新出现的、在动的东西
      2. **颜色通道** —— 抓白亮的泡沫，即使它停着不动

    为什么必须两路：只用背景减除时，一片随浪停在镜头前的泡沫会在
    几十帧内被学成"背景"，从此再也不报警；而泡沫恰恰是本项目
    最需要抓的类别（连江养殖区最突出、治理优先级最高）。
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        roi: list[list[float]] | None = None,
    ) -> None:
        self.config = config or {}
        self._roi = roi or []
        self._bg = cv2.createBackgroundSubtractorKNN(
            history=int(_get(config, "background", "history")),
            dist2Threshold=float(_get(config, "background", "dist2_threshold")),
            detectShadows=bool(_get(config, "background", "detect_shadows")),
        )
        self._kernel_open = self._kernel(_top(config, "morph_open"))
        self._kernel_close = self._kernel(_top(config, "morph_close"))
        self._frame_index = 0
        self._roi_mask: np.ndarray | None = None

    @staticmethod
    def _kernel(size: int) -> np.ndarray:
        size = max(1, int(size))
        return np.ones((size, size), np.uint8)

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def detect(self, frame: np.ndarray) -> list[dict[str, Any]]:
        """对一帧做检测，返回契约格式的检测列表。"""
        if frame is None or frame.size == 0:
            return []

        self._frame_index += 1
        h, w = frame.shape[:2]

        mask = self._foreground_mask(frame)
        mask = self._apply_roi(mask, (h, w))

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        results: list[dict[str, Any]] = []
        for contour in contours:
            det = self._build_detection(frame, contour)
            if det is not None:
                results.append(det)

        results.sort(key=lambda d: d["confidence"], reverse=True)
        return results

    def reset(self) -> None:
        """重置背景模型（摄像头云台转动、昼夜切换后必须调用）。"""
        self._bg = cv2.createBackgroundSubtractorKNN(
            history=int(_get(self.config, "background", "history")),
            dist2Threshold=float(_get(self.config, "background", "dist2_threshold")),
            detectShadows=bool(_get(self.config, "background", "detect_shadows")),
        )
        self._frame_index = 0
        self._roi_mask = None

    # ------------------------------------------------------------------
    # 候选区域
    # ------------------------------------------------------------------
    def _foreground_mask(self, frame: np.ndarray) -> np.ndarray:
        """背景减除 ∪ 白亮颜色通道。"""
        fg = self._bg.apply(frame)

        # ★ KNN 把阴影标成 127（detectShadows=True），只取 255 才是真前景。
        #   不剔阴影的话，船影、云影会被当成一大片目标，误报直接翻倍。
        _, fg_bin = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)

        if not bool(_get(self.config, "color", "enabled")):
            combined = fg_bin
        else:
            hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

            # 白亮通道：泡沫是白的、亮的，且可能停着不动
            white = cv2.inRange(
                hsv,
                np.array([0, 0, int(_get(self.config, "color", "v_min"))], np.uint8),
                np.array(
                    [179, int(_get(self.config, "color", "s_max")), 255], np.uint8
                ),
            )
            combined = cv2.bitwise_or(fg_bin, white)

            # 暗色通道：渔网/绳索比海面暗，且会被背景建模当成阴影剔掉
            if bool(_get(self.config, "color", "dark_enabled")):
                dark = cv2.inRange(
                    hsv,
                    np.array([0, 0, 0], np.uint8),
                    np.array(
                        [179, 255, int(_get(self.config, "color", "dark_v_max"))],
                        np.uint8,
                    ),
                )
                combined = cv2.bitwise_or(combined, dark)

        opened = cv2.morphologyEx(combined, cv2.MORPH_OPEN, self._kernel_open)
        closed = cv2.morphologyEx(opened, cv2.MORPH_CLOSE, self._kernel_close)
        return closed

    def _apply_roi(self, mask: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
        """把画面外的区域（天空、岸边建筑、固定缆绳）裁掉。

        ROI 为空表示全画面。多边形顶点是**相对坐标 0~1**，
        这样同一份配置在 720p 和 1080p 上都成立。
        """
        if not self._roi:
            return mask

        if self._roi_mask is None or self._roi_mask.shape[:2] != shape:
            h, w = shape
            pts = np.array(
                [[float(x) * w, float(y) * h] for x, y in self._roi], np.int32
            )
            canvas = np.zeros((h, w), np.uint8)
            cv2.fillPoly(canvas, [pts], 255)
            self._roi_mask = canvas

        return cv2.bitwise_and(mask, self._roi_mask)

    # ------------------------------------------------------------------
    # 单个候选 → 检测
    # ------------------------------------------------------------------
    def _build_detection(
        self, frame: np.ndarray, contour: np.ndarray
    ) -> dict[str, Any] | None:
        area = float(cv2.contourArea(contour))
        if area < float(_top(self.config, "min_area")):
            return None
        if area > float(_top(self.config, "max_area")):
            return None

        x, y, w, h = cv2.boundingRect(contour)
        if w <= 0 or h <= 0:
            return None

        perimeter = float(cv2.arcLength(contour, True))
        hull = cv2.convexHull(contour)
        hull_area = float(cv2.contourArea(hull))

        # 形状特征
        solidity = area / hull_area if hull_area > 0 else 0.0
        circularity = (4.0 * math.pi * area / (perimeter**2)) if perimeter > 0 else 0.0
        aspect = max(w, h) / max(1, min(w, h))

        if solidity < float(_top(self.config, "min_solidity")):
            return None
        if aspect > float(_top(self.config, "max_aspect")):
            return None

        # 颜色/纹理特征（只在轮廓内部取，避免混入背景海面）
        s_mean, v_mean, gray_std = self._color_features(frame, contour)

        cls, match = self._classify(s_mean, v_mean, aspect, solidity, circularity)
        confidence = self._confidence(
            cls, match, area, solidity, circularity, s_mean, v_mean
        )

        img_h, img_w = frame.shape[:2]
        return {
            "class": cls,
            "confidence": round(confidence, 4),
            "bbox": [
                _clamp_int(x, 0, img_w),
                _clamp_int(y, 0, img_h),
                _clamp_int(x + w, 0, img_w),
                _clamp_int(y + h, 0, img_h),
            ],
        }

    @staticmethod
    def _color_features(
        frame: np.ndarray, contour: np.ndarray
    ) -> tuple[float, float, float]:
        """取轮廓内部的平均饱和度、平均明度、灰度标准差。"""
        x, y, w, h = cv2.boundingRect(contour)
        patch = frame[y : y + h, x : x + w]
        if patch.size == 0:
            return 0.0, 0.0, 0.0

        obj_mask = np.zeros((h, w), np.uint8)
        cv2.drawContours(obj_mask, [contour], -1, 255, -1, offset=(-x, -y))

        hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)

        s_mean = float(cv2.mean(hsv[:, :, 1], mask=obj_mask)[0])
        v_mean = float(cv2.mean(hsv[:, :, 2], mask=obj_mask)[0])
        _, std = cv2.meanStdDev(gray, mask=obj_mask)
        return s_mean, v_mean, float(std[0][0])

    # ------------------------------------------------------------------
    # 分类与打分
    # ------------------------------------------------------------------
    def _classify(
        self,
        s_mean: float,
        v_mean: float,
        aspect: float,
        solidity: float,
        circularity: float,
    ) -> tuple[str, float]:
        """四类别规则分类，返回 (类别, 匹配度 0~1)。

        ★ 顺序即优先级，先命中先得 —— 这个顺序是按**可分性**排的：
        细长暗色（渔网绳索）最独特，先判；白亮团状（泡沫）次之；
        有彩度（塑料）再次；都不像就归 other。

        匹配度不是概率，是"离该类规则中心有多近"的可解释打分，
        现场答辩可以直接把阈值调给评委看。
        """
        fg_cfg = _get(self.config, "classifier", "fishing_gear")
        foam_cfg = _get(self.config, "classifier", "foam")
        plastic_cfg = _get(self.config, "classifier", "plastic")

        # 1) 废旧渔具：暗色 +（细长 **或** 破碎）
        #    ★ 用"或"而不是"且"：湿缆绳是规整的细长条（solidity≈1），
        #      破渔网是破碎的团（长宽比接近 1）。两者都是渔具，
        #      但在形状特征上各占一头，用"且"会漏掉其中一类。
        if v_mean <= float(fg_cfg["v_max"]) and (
            aspect >= float(fg_cfg["aspect_min"])
            or solidity <= float(fg_cfg["solidity_max"])
        ):
            elongation = _clip01((aspect - float(fg_cfg["aspect_min"])) / 3.0)
            broken = _clip01(
                (float(fg_cfg["solidity_max"]) - solidity)
                / float(fg_cfg["solidity_max"])
            )
            return "fishing_gear", _clip01(0.6 * max(elongation, broken) + 0.4 * broken)

        # 2) 泡沫：几乎不带颜色 + 白亮 + 团状
        if (
            s_mean <= float(foam_cfg["s_max"])
            and v_mean >= float(foam_cfg["v_min"])
            and circularity >= float(foam_cfg["circularity_min"])
        ):
            whiteness = _clip01(
                (v_mean - float(foam_cfg["v_min"])) / 60.0
            ) * _clip01((float(foam_cfg["s_max"]) - s_mean) / float(foam_cfg["s_max"]))
            # ★ 过曝惩罚：真实泡沫是"白但没到纯白"（V 约 220~245），
            #   而正午海面高光是纯白过曝（V≈255）。两者在 HSV 里
            #   其他维度几乎一样，只有最顶端这 10 个灰阶能分开。
            over_exposure = _clip01((v_mean - 250.0) / 5.0)
            whiteness *= 1.0 - 0.5 * over_exposure
            roundness = _clip01(circularity / 0.7)
            return "foam", _clip01(0.7 * whiteness + 0.3 * roundness)

        # 3) 塑料：有彩度
        if s_mean >= float(plastic_cfg["s_min"]):
            colorful = _clip01((s_mean - float(plastic_cfg["s_min"])) / 80.0)
            return "plastic", colorful

        return "other", 0.3

    def _confidence(
        self,
        cls: str,
        match: float,
        area: float,
        solidity: float,
        circularity: float,
        s_mean: float,
        v_mean: float,
    ) -> float:
        """伪置信度 = 基础分 + 匹配度浮动 − 反光惩罚，再夹到 [min, max]。

        ★ 为什么需要"伪置信度"：下游 `TemporalValidator` 用
        `min_confidence=0.45` 做第一道过滤。如果所有检测都给 0.9，
        这道过滤就形同虚设（历史上项目里"恒为真"的判断栽过两次）。

        ★ 但它压不死反光，这是诚实的边界：
        海面高光与白色泡沫在颜色、形状上高度重合，单帧可分性本来就低。
        真正的区分靠**时序** —— 高光随波浪闪现，凑不满 min_hits 就被饿死；
        泡沫持续存在，连续命中。所以反光惩罚的作用是把它的置信度
        **显著压到真实泡沫之下**（排序靠后），而不是假装能一帧判死。
        """
        conf_cfg = self.config.get("confidence") or DEFAULT_CONFIG["confidence"]
        base = float(conf_cfg.get("base", DEFAULT_CONFIG["confidence"]["base"]))
        span = float(conf_cfg.get("span", DEFAULT_CONFIG["confidence"]["span"]))
        low = float(conf_cfg.get("min", DEFAULT_CONFIG["confidence"]["min"]))
        high = float(conf_cfg.get("max", DEFAULT_CONFIG["confidence"]["max"]))

        shape_quality = _clip01(0.5 * (solidity / 0.9) + 0.5 * (circularity / 0.7))
        size_quality = self._size_quality(area, conf_cfg)

        score = 0.5 * match + 0.3 * shape_quality + 0.2 * size_quality
        value = base + span * score

        # 反光惩罚：极亮 + 很小 + 形状不规整 = 海面高光，不是泡沫
        glare = conf_cfg.get("glare") or DEFAULT_CONFIG["confidence"]["glare"]
        if (
            v_mean >= float(glare["v_threshold"])
            and area <= float(glare["area_max"])
            and circularity <= float(glare["circularity_max"])
        ):
            value -= float(glare["penalty"])

        # 兜底类别整体降一档，避免"什么都不像"的东西靠形状分混过高门限
        if cls == "other":
            value -= 0.1

        return _clamp(value, low, high)

    def _size_quality(self, area: float, conf_cfg: dict[str, Any]) -> float:
        size_cfg = conf_cfg.get("size") or DEFAULT_CONFIG["confidence"]["size"]
        min_area = float(_top(self.config, "min_area"))
        max_area = float(_top(self.config, "max_area"))

        if area < min_area * float(size_cfg["small_times"]):
            return float(size_cfg["small_factor"])
        if area > max_area * float(size_cfg["large_ratio"]):
            return float(size_cfg["large_factor"])
        return 1.0


def _clamp_int(value: float, low: int, high: int) -> int:
    v = int(round(value))
    return low if v < low else (high if v > high else v)
