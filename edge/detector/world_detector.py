"""开放词汇检测器（YOLO-World 通道）—— 零样本，不需要一张标注图。

它在本项目里的定位（★ 先读这一段，否则会误用）
----------------------------------------------
本通道**不是**主检测器，也**不是**"更准的 OpenCV"。它是：

- **能力演示**：现场说一个新词（"塑料瓶""渔网""泡沫浮球"），
  系统立刻能检出**这一类**，不需要重新采集、标注、训练；
- **对照评测**：与 ``CvDetector`` 跑同一批图，输出定性对照（检出情况 + 时延），
  默认**不进告警主链路**（``edge/config.yaml`` 的 ``detector.backend`` 默认 ``cv``）。

它**不承诺精度**。原因见下面的标定依据 —— 这是本模块存在的全部诚实性所在。

标定依据（本仓库历史实测，不是估计）
------------------------------------
``artifacts/marine-detections-yoloworld.json``（imgsz=1600，conf=0.02，
``yolov8s-worldv2.pt``）在 5 张演示原图上的结果：

==========================  ====  ========  ==========
图                           检出  最高置信   ≥0.45 的条数
==========================  ====  ========  ==========
beach.jpg                      8     0.123           0
foam.jpg                       3     0.048           0
midway.jpg                     7     0.413           0
net.jpg                        7     0.107           0
plastic.jpg                   16     0.294           0
==========================  ====  ========  ==========

``artifacts/vision-preview/`` 下四份 prompt 标定记录里，
**历史最高置信度为 0.184**（foam cup），标定阈值被压到 0.004~0.012 才拿到框。

结论有两层，都很重要：

1. **绝不能拿 0.45 当本通道的门限。** ``0.45`` 是 ``TemporalValidator`` 的
   *时序*门限，用来过滤 ``CvDetector`` 的伪置信度；开放词汇模型的输出分数
   是另一个尺度（中位数 0.04 量级）。把两者混为一谈，本通道的输出必然是空的。
   所以本模块有自己的 ``conf`` / ``temporal_min_confidence``（默认 **0.02**，
   取值依据见 ``DEFAULT_WORLD_CONFIG`` 的注释与 ``docs/decisions.md`` ADR-026）。
2. **姿态要放低。** 在当前可得的图上，它是"能指出候选"的水平，不是"能报精度"的水平。
   对外只讲"零样本、可现场加词"，不讲 precision / recall。

输出契约（与 ``CvDetector`` 完全一致，下游一行都不用改）
--------------------------------------------------------
    [{"class": "foam", "confidence": 0.72, "bbox": [x1, y1, x2, y2]}, ...]

``class`` 一定是 ``foam`` / ``plastic`` / ``fishing_gear`` / ``other`` 之一
（由 ``label_map`` 把提示词原文映射过来），``bbox`` 是设备原始分辨率下的
像素坐标、原点左上。

可选依赖的处理
--------------
torch / ultralytics / transformers **不在**边缘测试环境里，也不该被强行拖进来。
所以：

- 顶层不导入任何重依赖；
- 模型懒加载（第一次 ``detect`` 才建）；
- 依赖或权重缺失时 ``available`` 为 False、``last_error`` 给出可执行提示、
  ``detect()`` 返回空列表并把这一帧计入 ``stats()["unavailable_calls"]``
  ——**不抛异常拖垮主循环，也不静默假装"这一帧没有垃圾"**；
- 测试通过注入 ``runner`` 绕开真实模型，因此契约测试不需要 torch。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from label_map import (
    CONTRACT_CLASSES,
    DEMO_DEFAULT_CLASSES,
    map_prompt,
    unmapped_prompts,
)

BACKEND_NAME = "world"

# ★ 仓库根目录（本文件在 <root>/edge/detector/world_detector.py）。
#   用来把配置里的**相对权重路径**解析成确定的位置。
#
#   为什么必须这么做（这是实测踩到的坑，且后果很严重）：
#   `config.yaml` 里写的是裸文件名 `yolov8s-worldv2.pt`，而 ultralytics 是
#   按**当前工作目录**找权重的。于是：
#     - 从仓库根跑 → 找到本地文件，正常启动；
#     - 从 `edge/` 跑（`edge/main.py` 的常规用法）→ 找不到 →
#       ultralytics **静默转向网络下载**，连试 3 次，每次都在
#       `curl: (56) schannel: server closed abruptly` 上卡住，
#       **整个启动挂住 2 分钟以上且没有任何异常**。
#   现场演示正是"从 edge/ 启动"，所以这条会把演示直接卡死，
#   而且现象只是"程序没反应"，看不出是在下载权重。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_weights_path(raw: str | Path) -> Path:
    """把配置里的权重路径解析成绝对路径（相对路径按**仓库根**解析，不按 cwd）。

    裸文件名（如 ``yolov8s-worldv2.pt``）在从仓库根启动时恰好可用，
    所以这个缺陷平时不会暴露 —— 只有换一个 cwd 才现形（详见 PROJECT_ROOT 注释）。
    """
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else (PROJECT_ROOT / path)

# 原始检测的中间形态（runner 的返回约定）：
#   {"prompt": "plastic bottle", "confidence": 0.21, "bbox": [x1, y1, x2, y2]}
RawDetection = dict[str, Any]
# 注入式 runner：(frame, prompts, params) -> [RawDetection, ...]
Runner = Callable[[np.ndarray, list[str], dict[str, Any]], list[RawDetection]]


# ---------------------------------------------------------------------------
# 参数默认值
# ★ 与 edge/config.yaml 的 detector.world 段逐项对账（test_world_detector.py 里有断言）。
#   刻意**不复用** detector.py 的 DEFAULT_CONFIG：那是 OpenCV 通道的参数集，
#   两者的置信度尺度不同，混在一个字典里早晚会被谁改错。
# ---------------------------------------------------------------------------
DEFAULT_WORLD_CONFIG: dict[str, Any] = {
    # 权重与文本编码器
    "weights": "yolov8s-worldv2.pt",
    "clip_dir": "~/.cache/clip-vit-base-patch32",
    # 推理参数：CPU 是刻意的默认值（本机 GPU 与现有 torch 构建不匹配，见 README）
    "device": "cpu",
    "imgsz": 960,
    # ★ 模型侧打分门限。
    #   **不是 0.45** —— 那是 cv 通道的时序门限，两套尺度不同（ADR-024）。
    #
    #   为什么是 0.02：本通道的用途是"现场加一个词就能检出这一类"，
    #   所以默认门限必须让**四个契约类别都可报**，否则演示会出现
    #   "加词没反应"，而实际原因只是门限把它滤掉了。
    #   实测（artifacts/metrics/vision_backends_latest.json 的 threshold_probe，
    #   5 张 4K 真实照片、门限压到 0.001）逐类最高分：
    #       other 0.5824 / fishing_gear 0.0514 / plastic 0.0355 / foam 0.0260
    #   → foam 是最紧的一类，门限必须低于 0.026 才能让它可报，故取 0.02。
    #
    #   ★ 这是**为满足"四类皆可报"这一功能性要求选的演示工作点**，
    #     不是精度最优门限：独立测试集真值标注为 0，没有任何数据能标定它。
    #     严禁把 0.02 写成"最优"或"经标定"。现场如需换门限，
    #     用 `--world-conf` 或改 edge/config.yaml，两者都即时生效。
    "conf": 0.02,
    "iou": 0.45,
    "max_det": 60,
    # ★ 喂给 TemporalValidator 的门限。与 conf 同尺度 ——
    #   若沿用 cv 通道的 0.45，本通道的检测会在时序环节被全部丢掉。
    "temporal_min_confidence": 0.02,
    # 默认提示词：取自演示脚本的历史默认集（label_map.DEMO_DEFAULT_CLASSES 有对账测试）
    "prompt_classes": list(DEMO_DEFAULT_CLASSES),
    # ★ 是否允许 ultralytics 在本地找不到权重时**自动联网下载**。
    #   默认 False：现场网络不可预期，静默下载会让启动时间不可控
    #   （实测：联网重试 3 次、卡住 2 分多钟、最后仍可能失败，且全程无异常抛出）。
    #   部署/现场需要首次下载时，显式把 edge/config.yaml 里这一项改成 true，
    #   或直接按 ``resolve_weights_path`` 解析出的路径手工放好权重文件。
    "allow_weights_download": False,
}


def _cfg(config: dict[str, Any] | None, key: str) -> Any:
    """逐键取值，缺失回落到默认。

    ★ 逐键而不是整体 `config or DEFAULT_WORLD_CONFIG`：
    现场边缘盒上的 config.yaml 很可能是旧版（少几个键），
    整体回退会让运维在 yaml 里改的参数静默失效 —— 本项目反复出现的缺陷形状。
    """
    if config and key in config:
        return config[key]
    return DEFAULT_WORLD_CONFIG[key]


class WorldDetector:
    """开放词汇检测器。接口与 ``CvDetector`` 对齐（``detect`` / ``reset``）。

    用法（边缘主程序）：

        det = WorldDetector(config["world"], roi=cfg.get("roi"))
        if not det.available:
            print(det.last_error)   # 把"缺什么"说清楚，不要让它表现成"检测不到"
        for frame in stream:
            for d in det.detect(frame):
                ...  # 契约格式，可直接送时序校验

    现场加词（演示时的核心动作）：

        det.set_prompts(["plastic bottle", "fishing net", "foam buoy"])

    测试时注入 runner，不依赖 torch：

        WorldDetector(cfg, runner=lambda frame, prompts, params: [...])
    """

    backend_name = BACKEND_NAME

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        roi: list[list[float]] | None = None,
        runner: Runner | None = None,
    ) -> None:
        self.config = config or {}
        # 注入的 runner（测试夹具）：一旦注入就固定用它，set_prompts 不会作废它
        self._injected_runner: Runner | None = runner
        # 懒建的真实 runner 与其模型句柄（set_prompts 时对模型直接换词表，不重建）
        self._model_runner: Runner | None = None
        self._model: Any | None = None
        self._runner_error: str | None = None
        self._env_probe: tuple[bool, str] | None = None

        self._roi = roi or []
        self._roi_poly: np.ndarray | None = None
        self._roi_shape: tuple[int, int] | None = None

        prompts = _cfg(config, "prompt_classes") or []
        self._prompts: list[str] = [str(p) for p in prompts]
        # 提示词 → (契约类别, match_kind) 的缓存，set_prompts 时重建
        self._label_cache: dict[str, tuple[str, str]] = {}
        self._rebuild_label_cache()

        self._stats: dict[str, int] = {
            "calls": 0,
            "raw": 0,
            "kept": 0,
            "dropped_low_conf": 0,
            "dropped_roi": 0,
            "dropped_bad_box": 0,
            "unavailable_calls": 0,
            "fallback_other": 0,
        }

    # ------------------------------------------------------------------
    # 加载（把"能不能跑"的判定提前到启动阶段）
    # ------------------------------------------------------------------
    def load(self) -> bool:
        """强制构建模型一次，返回是否成功。

        ★ 为什么 `available` 不够
        -------------------------
        ``available`` 只做**依赖与权重的存在性探测**，不加载模型（要便宜、可在日志里随便调）。
        它因此发现不了下面这一类失败：**依赖齐、权重在，但模型建不起来**。

        真实案例（2026-09-27 实测）：`torch 2.5.1` + `ultralytics 8.4.35`，
        三项探测全绿，但 ultralytics 因 CVE-2025-32434 拒绝在 torch<2.6 上
        加载 ``.pt``（报错原文：*require users to upgrade torch to at least v2.6*）。
        于是 `available == True`、每帧都返回 0 检出。

        只问 `available` 就会把"这一路根本没跑起来"当成"画面里没有垃圾" ——
        两者在数字上一模一样。所以主程序启动时必须调本方法。

        失败原因会写进 ``last_error``，可用性相关的统计也随之更新。
        """
        if self._ensure_runner() is not None:
            return True
        if self._runner_error is None:
            self._runner_error = "模型未能构建（未知原因）"
        return False

    # ------------------------------------------------------------------
    # 状态自述
    # ------------------------------------------------------------------
    @property
    def prompts(self) -> tuple[str, ...]:
        """当前生效的提示词（现场加词后即是新的一套）。"""
        return tuple(self._prompts)

    @property
    def last_error(self) -> str | None:
        """最近一次不可用的原因；可用时为 None。"""
        return self._runner_error

    @property
    def available(self) -> bool:
        """本通道当前能否真正推理。

        ★ 惰性判定：注入 runner（测试）时为 True；否则只做依赖与权重的
        存在性探测，不加载模型，因此可以安全地放在启动日志里。

        ★ 探测失败时会把原因写进 ``last_error`` —— 否则主程序的失败提示
        只能打出"不可用：None"，等于没说。（这是实现时踩到过的坑：
        `available` 探到了原因却没存下来。）
        """
        if self._injected_runner is not None:
            return True
        ok, reason = self._probe_environment()
        if not ok and self._runner_error is None:
            self._runner_error = reason
        return ok

    @property
    def temporal_min_confidence(self) -> float:
        """本通道应当使用的时序门限。

        ★ 这是原方案最容易出错的一处：把 cv 通道的 0.45 套到本通道上，
        检测会在时序环节被全部丢掉，而现象只是"world 通道没结果"。
        主程序请从这里取值，不要在别处再写一遍配置键。
        """
        return float(_cfg(self.config, "temporal_min_confidence"))

    def stats(self) -> dict[str, Any]:
        """把"为什么少了检测框"拆成可解释的计数。

        没有这组计数，``fallback_other``（提示词没归宿）与
        ``dropped_low_conf``（门限太高）在现象上一模一样：某个类别恒为 0。
        """
        out: dict[str, Any] = dict(self._stats)
        out["prompts"] = list(self._prompts)
        out["unmapped_prompts"] = unmapped_prompts(self._prompts)
        out["conf"] = float(_cfg(self.config, "conf"))
        out["temporal_min_confidence"] = self.temporal_min_confidence
        out["available"] = self.available
        out["last_error"] = self._runner_error
        return out

    # ------------------------------------------------------------------
    # 提示词（现场加词）
    # ------------------------------------------------------------------
    def set_prompts(self, prompts: list[str] | tuple[str, ...]) -> None:
        """替换提示词集，下一次 ``detect`` 生效。

        现场演示就是这一句：评委说一个词，加进来，立刻能检出这一类。
        **不重训、不标注、不改代码** —— 这是本通道相对 ``CvDetector`` 的
        唯一硬性优势，也是它值得存在的理由。

        提示词会经 ``label_map`` 映射成四类；没有归宿的词回落 ``other``
        并计入 ``stats()["fallback_other"]``，不静默丢弃。
        """
        new_prompts = [str(p) for p in prompts]
        if not new_prompts:
            raise ValueError("提示词不能为空：开放词汇检测器至少需要一个提示词")

        self._prompts = new_prompts
        self._rebuild_label_cache()

        # 已建模则直接对模型换词表（比重新加载权重快得多）；
        # 注入式 runner 由测试夹具自行决定语义，这里不动它。
        if self._model is not None:
            self._model.set_classes(list(self._prompts))

    def _rebuild_label_cache(self) -> None:
        self._label_cache = {p: map_prompt(p) for p in self._prompts}

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def detect(self, frame: np.ndarray) -> list[dict[str, Any]]:
        """对一帧做检测，返回契约格式的检测列表。

        不可用时返回 ``[]`` 并把该次计入 ``unavailable_calls``。
        这样主循环不会因为缺依赖而崩，但统计里能立刻看出"这一路根本没跑"。
        """
        if frame is None or frame.size == 0:
            return []

        self._stats["calls"] += 1
        runner = self._ensure_runner()
        if runner is None:
            self._stats["unavailable_calls"] += 1
            return []

        h, w = frame.shape[:2]
        self._sync_roi((h, w))

        params = {
            "imgsz": int(_cfg(self.config, "imgsz")),
            "conf": float(_cfg(self.config, "conf")),
            "iou": float(_cfg(self.config, "iou")),
            "max_det": int(_cfg(self.config, "max_det")),
            "device": str(_cfg(self.config, "device")),
        }
        try:
            raw = runner(frame, list(self._prompts), params)
        except Exception as exc:  # noqa: BLE001 —— 单帧失败不该拖垮取流循环
            self._runner_error = f"本帧推理失败：{exc}"
            self._stats["unavailable_calls"] += 1
            return []

        self._stats["raw"] += len(raw)

        results: list[dict[str, Any]] = []
        for item in raw:
            det = self._build(item, w, h)
            if det is not None:
                results.append(det)

        # 稳定排序：置信度降序，同分按 bbox 字典序 —— 同一输入两次跑结果一致
        results.sort(key=lambda d: (-d["confidence"], d["bbox"]))
        self._stats["kept"] += len(results)
        return results

    def reset(self) -> None:
        """重置（与 ``CvDetector.reset`` 对齐）。

        本通道是无状态的（不像背景建模那样有历史），
        所以这里只清统计计数，**不动模型与提示词**。
        """
        for key in self._stats:
            self._stats[key] = 0

    # ------------------------------------------------------------------
    # 单条原始检测 → 契约
    # ------------------------------------------------------------------
    def _build(self, item: RawDetection, w: int, h: int) -> dict[str, Any] | None:
        prompt = str(item.get("prompt", ""))

        numeric = self._extract_numbers(item)
        if numeric is None:
            return None
        conf, x1, y1, x2, y2 = numeric

        if conf < float(_cfg(self.config, "conf")):
            self._stats["dropped_low_conf"] += 1
            return None

        if x2 <= x1 or y2 <= y1:
            self._stats["dropped_bad_box"] += 1
            return None

        # 裁剪到画面内（模型偶发越界框；下游按坐标做网格归并，越界会污染热力图）
        bx1 = _clamp_int(x1, 0, w)
        by1 = _clamp_int(y1, 0, h)
        bx2 = _clamp_int(x2, 0, w)
        by2 = _clamp_int(y2, 0, h)
        if bx2 <= bx1 or by2 <= by1:
            self._stats["dropped_bad_box"] += 1
            return None

        if not self._keep_by_roi(bx1, by1, bx2, by2):
            self._stats["dropped_roi"] += 1
            return None

        # 类别映射放在最后：只有真要输出的框才计入 fallback_other，
        # 否则"被门限丢掉的框"也会把提示词问题的计数抬高，读数就没法用了。
        cls, kind = self._label_for(prompt)
        if kind != "exact":
            self._stats["fallback_other"] += 1

        return {"class": cls, "confidence": round(conf, 4), "bbox": [bx1, by1, bx2, by2]}

    def _extract_numbers(
        self, item: RawDetection
    ) -> tuple[float, float, float, float, float] | None:
        """取出并校验 (confidence, x1, y1, x2, y2)；缺失/非数/非有限值返回 None。"""
        try:
            conf = float(item.get("confidence", 0.0))
            bbox = item.get("bbox", ())
            x1, y1, x2, y2 = (float(v) for v in bbox)
        except (TypeError, ValueError):
            self._stats["dropped_bad_box"] += 1
            return None
        for value in (conf, x1, y1, x2, y2):
            if not math.isfinite(value):
                self._stats["dropped_bad_box"] += 1
                return None
        return conf, x1, y1, x2, y2

    def _label_for(self, prompt: str) -> tuple[str, str]:
        match = self._label_cache.get(prompt)
        if match is None:
            # 模型给出的 prompt 不在当前词表里（例如词表刚被外部改过）
            match = map_prompt(prompt)
            self._label_cache[prompt] = match
        return match

    # ------------------------------------------------------------------
    # ROI
    # ------------------------------------------------------------------
    def _sync_roi(self, shape: tuple[int, int]) -> None:
        """按当前帧尺寸构建 ROI 多边形（相对坐标 → 像素），尺寸变了就重建。"""
        if not self._roi:
            self._roi_poly = None
            self._roi_shape = None
            return
        if self._roi_poly is not None and self._roi_shape == shape:
            return
        h, w = shape
        self._roi_poly = np.array(
            [[float(x) * w, float(y) * h] for x, y in self._roi], dtype=np.float32
        )
        self._roi_shape = shape

    def _keep_by_roi(self, x1: int, y1: int, x2: int, y2: int) -> bool:
        """框中心是否落在 ROI 多边形内。

        ★ 与 ``CvDetector`` 的 ROI 语义刻意一致（相对坐标多边形、空表示全画面），
        但**判定方式不同**：那边是在掩膜上逐像素裁剪，这边是按框中心判断。
        所以换通道时 ROI 的边界行为会有细微差别 —— 这条应当写进部署说明，
        而不是让运维在现场自己发现。
        """
        if self._roi_poly is None:
            return True
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        return cv2.pointPolygonTest(self._roi_poly, (cx, cy), False) >= 0

    # ------------------------------------------------------------------
    # runner：真实模型 / 注入
    # ------------------------------------------------------------------
    def _probe_environment(self) -> tuple[bool, str]:
        """探测依赖与权重是否就绪（不加载模型），结果缓存一次。"""
        if self._env_probe is not None:
            return self._env_probe

        try:
            from clip_shim import (  # noqa: PLC0415
                clip_available,
                clip_dir_status,
                ultralytics_available,
            )
        except ImportError as exc:  # pragma: no cover - 同目录模块，正常不会缺
            self._env_probe = (False, f"clip_shim 不可导入：{exc}")
            return self._env_probe

        deps_ok, deps_reason = clip_available()
        if not deps_ok:
            self._env_probe = (False, deps_reason)
            return self._env_probe

        clip_dir = Path(str(_cfg(self.config, "clip_dir"))).expanduser()
        weights_ok, weights_reason = clip_dir_status(clip_dir)
        if not weights_ok:
            self._env_probe = (False, weights_reason)
            return self._env_probe

        # ★ 检测权重也要在这一步探到。只探 CLIP 目录会漏掉"权重不在"，
        #   而那正是"从 edge/ 启动时静默联网下载"的入口（见 PROJECT_ROOT 注释）。
        #   探到就能在启动阶段给出准确原因，而不是等到 load() 才失败。
        det_weights = resolve_weights_path(_cfg(self.config, "weights"))
        if not det_weights.exists() and not bool(
            _cfg(self.config, "allow_weights_download")
        ):
            self._env_probe = (
                False,
                f"权重文件不存在：{det_weights}"
                "（相对路径按仓库根解析，不按当前工作目录；"
                "如需联网下载请置 detector.world.allow_weights_download=true）",
            )
            return self._env_probe

        ultra_ok, ultra_reason = ultralytics_available()
        if not ultra_ok:
            self._env_probe = (False, ultra_reason)
            return self._env_probe

        self._env_probe = (True, f"就绪（{deps_reason}；{ultra_reason}）")
        return self._env_probe

    def _ensure_runner(self) -> Runner | None:
        if self._injected_runner is not None:
            return self._injected_runner
        if self._model_runner is not None:
            return self._model_runner
        if self._runner_error is not None:
            return None  # 已经失败过，不重复尝试（避免每帧都去 import 并打日志）

        ok, reason = self._probe_environment()
        if not ok:
            self._runner_error = reason
            return None

        try:
            self._model_runner = self._build_ultralytics_runner()
        except Exception as exc:  # noqa: BLE001
            self._runner_error = f"模型加载失败：{exc}"
            return None
        return self._model_runner

    def _build_ultralytics_runner(self) -> Runner:
        """构造真实 runner：装 CLIP 垫片 → 建 YOLO-World → 子类词表。

        只在依赖齐备时被调用。
        """
        from clip_shim import install_clip_shim  # noqa: PLC0415

        clip_dir = Path(str(_cfg(self.config, "clip_dir"))).expanduser()
        install_clip_shim(clip_dir)

        # ★ 权重路径必须在这里解析成绝对路径后再交给 ultralytics。
        #   传裸文件名等于把"用哪个权重"这件事交给 cwd 决定（详见 PROJECT_ROOT 注释），
        #   从 edge/ 启动会退化成联网下载并把启动卡住数分钟。
        weights_path = resolve_weights_path(_cfg(self.config, "weights"))
        if not weights_path.exists():
            if not bool(_cfg(self.config, "allow_weights_download")):
                # 明确失败，而不是让 ultralytics 静默下载：
                # 现场没有网络时，"下载中"和"卡死"在观测上无法区分。
                raise FileNotFoundError(
                    f"权重文件不存在：{weights_path}。"
                    "（相对路径按仓库根解析，不按当前工作目录）"
                    "请把权重放到该路径，或把 edge/config.yaml 的 "
                    "detector.world.allow_weights_download 设为 true 允许联网下载。"
                )

        from ultralytics import YOLOWorld  # noqa: PLC0415

        # 本地存在时必须传字符串形式的**绝对**路径：
        # ultralytics 见到绝对路径就不会再去解析/下载。
        model = YOLOWorld(str(weights_path))
        model.set_classes(list(self._prompts))
        self._model = model

        def runner(
            frame: np.ndarray, prompts: list[str], params: dict[str, Any]
        ) -> list[RawDetection]:
            result = model.predict(
                frame,
                conf=params["conf"],
                iou=params["iou"],
                imgsz=params["imgsz"],
                max_det=params["max_det"],
                device=params["device"],
                verbose=False,
            )[0]

            out: list[RawDetection] = []
            if result.boxes is None:
                return out
            boxes = result.boxes.xyxy.detach().cpu().tolist()
            confs = result.boxes.conf.detach().cpu().tolist()
            class_ids = result.boxes.cls.detach().cpu().int().tolist()
            for bbox, conf, cid in zip(boxes, confs, class_ids, strict=True):
                out.append(
                    {
                        "prompt": prompts[cid],
                        "confidence": float(conf),
                        "bbox": [float(v) for v in bbox],
                    }
                )
            return out

        return runner


def _clamp_int(value: float, low: int, high: int) -> int:
    v = int(round(value))
    return low if v < low else (high if v > high else v)


def contract_classes() -> tuple[str, ...]:
    """本模块产出的类别集合（供对账测试直接引用，避免测试里再抄一遍）。"""
    return CONTRACT_CLASSES
