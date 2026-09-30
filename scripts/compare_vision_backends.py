#!/usr/bin/env python
"""定性对照：cv（OpenCV 传统视觉） vs world（开放词汇零样本）。

★ 先说清这是什么、不是什么
---------------------------
这是**定性对照**，不是精度评测。

本仓库的独立测试集清单处于 ``status=planned``、真值标注为 0
（``ml/configs/seasight.yaml`` 的 stats 全 0），
因此 **precision / recall / F1 在真实数据上不可计算** ——
真值缺失时，这几个指标的分母就是零。
定量评测请走 ``ml/scripts/evaluate_opencv.py``：
它带清单门禁，数据未就绪时会明确拒绝评测（输出 ``not_evaluated``）而不是编造数字。
本脚本刻意独立、且**不产出任何真实数据集的精度字段**。

两组对照，各自回答不同的问题
----------------------------
1. **静态照片组**（默认 ``.wp-demo-originals/*.jpg``）
   问的是"把一张图丢进去，两条通道各看到什么"。
   ★ 必须注意：``cv`` 通道是**固定机位背景建模**方案，
   单张 4K 照片不在它的适用范围内（整幅前景会被 ``max_area`` 过滤掉）。
   该组里 cv 的条数**不能**当作它能力的度量 —— 脚本会把这一点算出来写进 caveats。

2. **合成序列组**（``--sequence``）
   问的是"同一条视频流里，两条通道各检出什么、多快"。
   这是两条通道**都处在正常工作条件**下的对照：
   ``cv`` 有连续帧可建模，``world`` 逐帧零样本推理。
   合成帧的 3 个泡沫目标是**程序化真值**（位置已知），
   因此这一组额外给出"命中/漏检"计数 —— 口径是**合成数据（E1/E2）**，
   严禁外推为真实海域表现。

两条通道的口径差异（必须知道，否则会误读结果）
----------------------------------------------
- ``cv`` 通道是**背景建模**：靠"与背景的差异"找目标，因此需要预热。
  对一张静止的单图，若不预热，首帧会被判成整幅前景。
- ``world`` 通道是**零样本开放词汇**：不需要预热，也不需要这些图出现在训练集里。
  换一个词就能检出另一类 —— 这是它唯一相对 cv 通道的硬性优势。
- 两条通道的**置信度尺度不同**（cv 是 0.30~0.95 的伪置信度；world 低一个量级，
  本仓库历史实测最高 0.184）。两份置信度**不可横向比较**。

用法：
    python scripts/compare_vision_backends.py                     # 静态照片组
    python scripts/compare_vision_backends.py --sequence           # 合成序列组
    python scripts/compare_vision_backends.py --sequence --frames 120
    python scripts/compare_vision_backends.py --only-world --sequence
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
EDGE = ROOT / "edge"
sys.path.insert(0, str(EDGE / "detector"))
sys.path.insert(0, str(EDGE))
sys.path.insert(0, str(ROOT / "ml" / "scripts"))

# 默认图集：仓库里已有的演示原图（5 张，含泡沫/渔网/塑料/混合场景）
DEFAULT_IMAGE_DIR = ROOT / ".wp-demo-originals"
DEFAULT_OUTPUT = ROOT / "artifacts" / "metrics" / "vision_backends_latest.json"

# cv 通道在静态图上的预热帧数（与 edge/detector/test_detector.py 的 _warm 同量级）
WARMUP_FRAMES = 15

# 目标与检测的匹配门限：与 edge/config.yaml 的 temporal.min_iou 同口径
MATCH_IOU = 0.25

NOT_EVALUATED = "not_evaluated"
EVALUATED = "evaluated"

SYNTHETIC_EVIDENCE_NOTE = (
    "合成序列 + 程序化真值，证据等级 E1/E2。合成帧里的泡沫是绘制出来的圆，"
    "与真实海面目标的表观分布不同，命中率不得外推为真实海域表现。"
)

# 诊断门限：近乎为零，确保任何非零输出都会留下。
# 用途只有一个 —— 回答"到底是门限把框丢了，还是模型本来就没输出"。
# ★ 这一路的产物是**诊断**，永远不得计入 detections_total / gt_hits。
PROBE_CONF = 0.001

# 分数分档（现场标定 conf 时用来判断"默认值是否卡在分数断层上"）
SCORE_BUCKETS = (0.10, 0.05, 0.02, 0.01)


# ----------------------------------------------------------------------
# 合成空海面（仅用于给 cv 通道预热背景模型）
# ----------------------------------------------------------------------
def _sea_frame(seed: int, size: tuple[int, int] = (360, 640)) -> np.ndarray:
    """灰蓝海面背景，亮度压在 V<150（否则背景自己会被"白亮"通道当泡沫）。"""
    rng = np.random.default_rng(seed)
    base = np.zeros((size[0], size[1], 3), np.uint8)
    base[:] = (110, 100, 82)
    noise = rng.normal(0, 4, base.shape)
    return np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def _load_synthetic_sea():
    """从 edge/main.py 取 SyntheticSea（不复制一份，避免两处演化）。

    用 importlib 按文件路径加载并起一个独特模块名，
    避免与标准库/第三方里任何叫 ``main`` 的模块撞名。
    """
    spec = importlib.util.spec_from_file_location("_seasight_edge_main", EDGE / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.SyntheticSea


def build_synthetic_sequence(frames: int, seed: int = 42) -> tuple[list[np.ndarray], list[list[list[float]]]]:
    """产出 (帧列表, 每帧的程序化真值框列表)。

    真值取 SyntheticSea 里 3 个泡沫团的外接方框 —— 它们的位置是生成器自己
    算出来的，不经过任何模型，所以是真值而不是"另一个检测器的输出"。
    """
    SyntheticSea = _load_synthetic_sea()
    gen = iter(SyntheticSea(seed=seed))
    images: list[np.ndarray] = []
    truths: list[list[list[float]]] = []
    for _ in range(frames):
        frame = next(gen)
        images.append(frame)
        boxes: list[list[float]] = []
        for b in gen.blobs:
            x, y, r = float(b["x"]), float(b["y"]), float(b["r"])
            boxes.append([x - r, y - r, x + r, y + r])
        truths.append(boxes)
    return images, truths


# ----------------------------------------------------------------------
# 参数
# ----------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="cv vs world 定性对照（不含真实数据精度指标）")
    parser.add_argument("--image", action="append", help="图片路径；可重复。默认用 .wp-demo-originals/*.jpg")
    parser.add_argument("--limit", type=int, default=0, help="静态组最多处理多少张（0 = 全部）")
    parser.add_argument("--sequence", action="store_true", help="跑合成序列组（两条通道同帧对照）")
    parser.add_argument("--frames", type=int, default=60, help="合成序列帧数（默认 60）")
    parser.add_argument("--only", action="append", choices=["cv", "world"],
                        help="只跑指定通道；可重复。默认两个都跑")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="JSON 输出路径")
    parser.add_argument("--world-prompt", action="append",
                        help="给开放词汇通道追加提示词（可重复）")
    parser.add_argument("--world-conf", type=float,
                        help="覆盖开放词汇通道的打分门限（现场标定用；默认读 config.yaml）")
    return parser.parse_args()


def resolve_images(args: argparse.Namespace) -> list[Path]:
    if args.image:
        paths = [Path(p).expanduser() for p in args.image]
    else:
        if not DEFAULT_IMAGE_DIR.exists():
            return []
        paths = sorted(DEFAULT_IMAGE_DIR.glob("*.jpg")) + sorted(DEFAULT_IMAGE_DIR.glob("*.png"))
    paths = [p if p.is_absolute() else (ROOT / p) for p in paths]
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise SystemExit("图片不存在：" + ", ".join(str(p) for p in missing))
    if args.limit:
        paths = paths[: args.limit]
    return paths


def load_detector_config() -> dict[str, Any]:
    """从 edge/config.yaml 读 detector 段（与运行时同一份配置，不另抄一份默认值）。"""
    import yaml  # noqa: PLC0415

    cfg = yaml.safe_load((EDGE / "config.yaml").read_text(encoding="utf-8")) or {}
    return cfg.get("detector") or {}


def build_world(cfg: dict[str, Any], extra_prompts: list[str],
                conf_override: float | None = None):
    from world_detector import WorldDetector  # noqa: PLC0415

    world_cfg = dict(cfg.get("world") or {})
    if extra_prompts:
        world_cfg["prompt_classes"] = list(world_cfg.get("prompt_classes") or []) + extra_prompts
    if conf_override is not None:
        # 现场标定：只改打分门限，时序门限跟着走（两者必须同尺度，见 ADR-024）
        world_cfg["conf"] = float(conf_override)
        world_cfg["temporal_min_confidence"] = float(conf_override)
    return WorldDetector(config=world_cfg, roi=[]), world_cfg


def _iou(a: list[float], b: list[float]) -> float:
    """复用 ml/scripts/evaluate_opencv.py 的 IoU 实现（同一套口径，不另写一份）。"""
    from evaluate_opencv import box_iou  # noqa: PLC0415

    return float(box_iou(a, b))


def _match_truth(truth_boxes: list[list[float]], dets: list[dict[str, Any]], iou_min: float) -> int:
    """这一帧有几个真值目标被检测命中（贪心：一个检测只能命中一个真值）。"""
    used: set[int] = set()
    hits = 0
    for gt in truth_boxes:
        for idx, det in enumerate(dets):
            if idx in used:
                continue
            if _iou(gt, [float(v) for v in det["bbox"]]) >= iou_min:
                used.add(idx)
                hits += 1
                break
    return hits


# ----------------------------------------------------------------------
# 诊断通道：把"0 检出"拆成两种完全不同的原因
# ----------------------------------------------------------------------
def world_threshold_probe(frames: list[np.ndarray], cfg: dict[str, Any],
                          extra_prompts: list[str], conf_override: float | None = None,
                          max_frames: int = 3) -> dict[str, Any]:
    """用近乎为零的门限重跑少数几帧，产出分数分布与原始输出计数。

    ★ 为什么必须有这一路
    --------------------
    "world 通道检出 0 条"有两种成因，在最终数字上**一模一样**：

    - **门限把框丢了**：模型输出了框，但都低于 ``conf``。
      特征是 ``raw`` 大、``kept`` 小、``dropped_low_conf`` 大。调门限就能救。
    - **模型本来就没输出**：模型对这类画面无话可说。
      特征是 ``raw == 0``、``dropped_*`` 全为 0。**调门限救不了**。

    只有把这两个数摆在一起，读数的人才能分辨。
    否则"这一路跑起来了但认不出"会被读成"这一路失效了"（或反过来）。

    本函数还顺带给出最高分 ``top_confidence`` —— 现场标定 ``conf`` 时，
    要的是"默认值卡在断层上"还是"卡在长尾里"，靠这个数判断。
    """
    out: dict[str, Any] = {
        "probe_conf": PROBE_CONF,
        "note": (
            "诊断用：门限压到近乎为零再跑一次，用来区分"
            "『门限丢了框』与『模型没输出』。本结果不计入 detections_total。"
        ),
    }

    # ★ 空样本必须在加载模型之前就判掉。否则 raw=0 会被下面的分支读成
    #   "模型对这批画面无输出" —— 而真相是"压根没喂进去任何一帧"。
    #   这两种情况对现场的处置完全不同，绝不能共用同一个结论。
    #   （放在这里而不是 load() 之后：没必要为了一个空输入花 0.4 秒建模型。）
    sample = frames[:max_frames]
    if not sample:
        out.update(available=False, reason="没有可用的诊断帧（输入为空或全部不可读）")
        out["diagnosis"] = "未能诊断：没有可用的诊断帧，本次不构成任何关于模型的结论。"
        return out

    det, _ = build_world(cfg, extra_prompts, PROBE_CONF)
    # 诊断同样要两步查：模型建不起来时 raw=0 是假的，必须显式标注
    if not det.available or not det.load():
        out.update(available=False, reason=det.last_error or "未知原因")
        return out

    confs: list[float] = []
    per_class: dict[str, list[float]] = {}
    for frame in sample:
        for det_item in det.detect(frame):
            conf = float(det_item["confidence"])
            confs.append(conf)
            per_class.setdefault(str(det_item["class"]), []).append(conf)

    stats = det.stats()
    buckets = {f">={b}": sum(1 for c in confs if c >= b) for b in SCORE_BUCKETS}
    out.update(
        available=True,
        frames=len(sample),
        raw=stats["raw"],
        kept=stats["kept"],
        dropped_low_conf=stats["dropped_low_conf"],
        top_confidence=round(max(confs), 4) if confs else 0.0,
        score_buckets={**buckets, "total": len(confs)},
        # 逐类最高分：直接回答"这个类别到底能不能过成品门限"。
        # 没有这一列，读数的人只能看到总数，看不出某一类恒为 0 是模型认不出
        # 还是门限卡掉了 —— 这两种情况对现场的应对方式完全不同。
        class_top=None if not per_class else {
            cls: round(max(vals), 4) for cls, vals in sorted(per_class.items())
        },
        class_counts=None if not per_class else {
            cls: len(vals) for cls, vals in sorted(per_class.items())
        },
    )
    if stats["raw"] == 0:
        out["diagnosis"] = (
            "模型对这批画面**原始输出为 0**（不是因为门限）："
            "dropped_* 全为 0。调低 conf 也救不回来。"
        )
    elif not confs:
        out["diagnosis"] = (
            f"模型有原始输出（raw={stats['raw']}），但全部低于诊断门限 "
            f"{PROBE_CONF} —— 说明输出的是退化框，需查模型与权重。"
        )
    else:
        out["diagnosis"] = (
            f"模型有真实输出，最高分 {max(confs):.4f}；"
            "是否被成品的 conf 丢掉，看 dropped_low_conf。"
        )
    return out


def _probe_frames_from_paths(images: list[Path], max_frames: int = 3) -> list[np.ndarray]:
    frames = []
    for path in images[:max_frames]:
        img = cv2.imread(str(path))
        if img is not None:
            frames.append(img)
    return frames


# ----------------------------------------------------------------------
# 静态照片组
# ----------------------------------------------------------------------
def run_cv_stills(images: list[Path], cfg: dict[str, Any]) -> dict[str, Any]:
    from detector import CvDetector  # noqa: PLC0415

    out: dict[str, Any] = {
        "backend": "cv",
        "available": True,
        "status": EVALUATED,
        "input": "still_images",
        "warmup_frames": WARMUP_FRAMES,
    }
    per_image: list[dict[str, Any]] = []
    total = 0

    for path in images:
        img = cv2.imread(str(path))
        if img is None:
            per_image.append({"name": path.name, "status": "unreadable"})
            continue

        # ★ 每张图都用全新检测器 + 合成空海面预热：
        #   否则上一张图会污染下一张的背景模型，结果不可复现。
        det = CvDetector(config=cfg, roi=[])
        for i in range(WARMUP_FRAMES):
            det.detect(_sea_frame(seed=i))

        t0 = time.perf_counter()
        dets = det.detect(img)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        per_image.append(
            {
                "name": path.name,
                "width": int(img.shape[1]),
                "height": int(img.shape[0]),
                "count": len(dets),
                "class_counts": dict(Counter(d["class"] for d in dets)),
                "max_confidence": round(max((d["confidence"] for d in dets), default=0.0), 4),
                "latency_ms": round(latency_ms, 2),
            }
        )
        total += len(dets)

    out["per_image"] = per_image
    out["detections_total"] = total
    out["images_with_detection"] = sum(1 for r in per_image if r.get("count"))
    out["images_total"] = len(per_image)
    _add_latency_summary(out)
    return out


def run_world_stills(images: list[Path], cfg: dict[str, Any], extra_prompts: list[str],
                     conf_override: float | None = None) -> dict[str, Any]:
    det, world_cfg = build_world(cfg, extra_prompts, conf_override)

    out: dict[str, Any] = {
        "backend": "world",
        "input": "still_images",
        "conf": world_cfg.get("conf"),
        "weights": world_cfg.get("weights"),
        "imgsz": world_cfg.get("imgsz"),
        "prompts": list(det.prompts),
    }

    # ★ 两步都要查：available 只看依赖与权重是否存在，发现不了
    #   "依赖齐、权重在，但模型建不起来"（实测：torch<2.6 时 ultralytics 拒载 .pt）。
    #   只查 available 会把这类失败写成 evaluated + 0 检出 —— 那就等于把
    #   "这一路没跑起来"伪装成"没有检到东西"。
    if not det.available or not det.load():
        out.update(
            available=False,
            status=NOT_EVALUATED,
            reason=det.last_error or "未知原因",
            detections_total=None,
            images_with_detection=None,
            images_total=len(images),
        )
        return out

    out["available"] = True
    out["status"] = EVALUATED
    per_image: list[dict[str, Any]] = []
    total = 0

    for path in images:
        img = cv2.imread(str(path))
        if img is None:
            per_image.append({"name": path.name, "status": "unreadable"})
            continue

        t0 = time.perf_counter()
        dets = det.detect(img)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        per_image.append(
            {
                "name": path.name,
                "width": int(img.shape[1]),
                "height": int(img.shape[0]),
                "count": len(dets),
                "class_counts": dict(Counter(d["class"] for d in dets)),
                "max_confidence": round(max((d["confidence"] for d in dets), default=0.0), 4),
                "latency_ms": round(latency_ms, 2),
            }
        )
        total += len(dets)

    out["per_image"] = per_image
    out["detections_total"] = total
    out["images_with_detection"] = sum(1 for r in per_image if r.get("count"))
    out["images_total"] = len(per_image)
    out["detector_stats"] = det.stats()
    # ★ 成品门限下的读数必须配一张"压到近零门限"的分数分布，
    #   否则 conf 这个数字没有任何可追溯的依据（为什么是 0.10 而不是 0.05？）
    #   本组图少（默认 5 张），整批都过一遍，分布才完整 ——
    #   只过前几张会漏掉"某一类只在某一张图上出现"的类别（实测：fishing_gear）。
    _probe_imgs = _probe_frames_from_paths(images, max_frames=min(len(images), 8))
    out["threshold_probe"] = world_threshold_probe(
        _probe_imgs, cfg, extra_prompts, conf_override, max_frames=len(_probe_imgs))
    _add_latency_summary(out)
    return out


# ----------------------------------------------------------------------
# 合成序列组
# ----------------------------------------------------------------------
def _sequence_block(backend: str, images: list[np.ndarray], truths: list[list[list[float]]],
                    det: Any, needs_warmup: bool, meta: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "backend": backend,
        "input": "synthetic_sequence",
        "status": EVALUATED,
        "frames": len(images),
        "gt_targets_total": sum(len(t) for t in truths),
        "match_iou": MATCH_IOU,
        "evidence_level": "E1/E2",
        "not_generalizable": SYNTHETIC_EVIDENCE_NOTE,
        **meta,
    }

    if needs_warmup:
        for i in range(WARMUP_FRAMES):
            det.detect(_sea_frame(seed=i))

    lat: list[float] = []
    per_frame: list[dict[str, Any]] = []
    det_total = 0
    frames_with_det = 0
    gt_hit = 0

    for frame, truth in zip(images, truths, strict=True):
        t0 = time.perf_counter()
        dets = det.detect(frame)
        lat.append((time.perf_counter() - t0) * 1000.0)

        hits = _match_truth(truth, dets, MATCH_IOU)
        gt_hit += hits
        det_total += len(dets)
        if dets:
            frames_with_det += 1
        per_frame.append(
            {
                "detections": len(dets),
                "classes": dict(Counter(d["class"] for d in dets)),
                "gt_hits": hits,
                "gt_targets": len(truth),
            }
        )

    out["detections_total"] = det_total
    out["frames_with_detection"] = frames_with_det
    out["gt_targets_hit"] = gt_hit
    out["gt_hit_rate"] = round(gt_hit / out["gt_targets_total"], 4) if out["gt_targets_total"] else None
    out["latency_ms"] = {
        "mean": round(sum(lat) / len(lat), 2) if lat else None,
        "max": round(max(lat), 2) if lat else None,
        "min": round(min(lat), 2) if lat else None,
    }
    out["per_frame_sample"] = per_frame[:5]
    if hasattr(det, "stats"):
        out["detector_stats"] = det.stats()
    return out


def run_cv_sequence(images: list[np.ndarray], truths: list[list[list[float]]], cfg: dict[str, Any]) -> dict[str, Any]:
    from detector import CvDetector  # noqa: PLC0415

    # ★ 一个检测器贯穿整段序列：这才是它的真实工作方式（背景模型随时间收敛）
    det = CvDetector(config=cfg, roi=[])
    return _sequence_block("cv", images, truths, det, needs_warmup=False, meta={
        "warmup_frames": 0,
        "note": "一个检测器贯穿全序列，背景模型按真实用法逐帧收敛",
    })


def run_world_sequence(images: list[np.ndarray], truths: list[list[list[float]]],
                       cfg: dict[str, Any], extra_prompts: list[str],
                       conf_override: float | None = None) -> dict[str, Any]:
    det, world_cfg = build_world(cfg, extra_prompts, conf_override)
    meta = {
        "conf": world_cfg.get("conf"),
        "weights": world_cfg.get("weights"),
        "imgsz": world_cfg.get("imgsz"),
        "prompts": list(det.prompts),
    }
    if not det.available or not det.load():
        return {
            "backend": "world",
            "input": "synthetic_sequence",
            "available": False,
            "status": NOT_EVALUATED,
            "reason": det.last_error or "未知原因",
            "frames": len(images),
            "gt_targets_total": sum(len(t) for t in truths),
            "detections_total": None,
            "gt_targets_hit": None,
            "gt_hit_rate": None,
            **meta,
        }
    block = _sequence_block("world", images, truths, det, needs_warmup=False, meta={"available": True, **meta})
    # ★ 检出为 0 时补一次诊断。"0"有两种读法，不给出成因就等于让读数的人猜：
    #   是模型认不出这批合成画面，还是这批画面本来就没有模型认识的东西。
    if not block.get("detections_total"):
        probe = world_threshold_probe(images, cfg, extra_prompts, conf_override)
        block["threshold_probe"] = probe
        if probe.get("available") and probe.get("raw") == 0:
            block["caveats"] = [
                "world 通道在本组检出 0 条，且**不是被门限丢掉的**："
                f"门限压到 {PROBE_CONF} 重跑同样 raw=0、dropped_* 全为 0。"
                "合成帧是程序化绘制的纯色圆盘（背景单色 + 3 个色块圆），"
                "缺少开放词汇零样本模型赖以判别的外观纹理，属于其输入分布之外。"
                "因此本组这 0 条**不能**读作'开放词汇通道无效'，"
                "它只说明**这批合成真值不适合用来考察该通道**。"
                "开放词汇通道的实际表现请看静态照片组（那里有真实纹理）。",
            ]
    return block


def _add_latency_summary(block: dict[str, Any]) -> None:
    lat = [r["latency_ms"] for r in block.get("per_image", []) if "latency_ms" in r]
    if not lat:
        return
    block["latency_ms"] = {
        "mean": round(sum(lat) / len(lat), 2),
        "max": round(max(lat), 2),
        "min": round(min(lat), 2),
    }


# ----------------------------------------------------------------------
# 版本指纹（与 evaluate_opencv.py 同一套口径）
# ----------------------------------------------------------------------
def code_version() -> dict[str, str]:
    try:
        value = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        source = "git"
    except Exception:  # noqa: BLE001
        value = "no-git"
        source = "fallback"
    fingerprint = hashlib.sha256()
    for rel in ("edge/detector/detector.py", "edge/detector/world_detector.py",
                "edge/detector/label_map.py", "edge/config.yaml"):
        f = ROOT / rel
        fingerprint.update(rel.encode())
        if f.exists():
            fingerprint.update(f.read_bytes())
    return {"source": source, "value": value, "fingerprint": fingerprint.hexdigest()}


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def main() -> int:
    args = parse_args()
    cfg = load_detector_config()
    wanted = args.only or ["cv", "world"]

    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "kind": "qualitative_backend_comparison",
        "not_precision_recall": (
            "本文件不含真实数据集的 precision/recall/F1。独立测试集清单 status=planned、"
            "真值标注为 0，这几个指标的分母为零，任何精度数字都会是编造的。"
            "定量评测请走 ml/scripts/evaluate_opencv.py（带清单门禁）。"
        ),
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "code_version": code_version(),
        "caveats": [
            "两条通道的置信度尺度不同（cv 为 0.30~0.95 的伪置信度，world 低一个量级），"
            "max_confidence 不可横向比较。",
            "本对照只说明检出条数、类别分布与时延，不构成精度、召回或可用性结论。",
            # 2026-09-27 实测：同一批帧、全新实例，60 帧窗口的 cv 检出总数
            # 在 171~173 之间波动（约 ±2 / 1%）。定位过程：逐帧比对首次分歧出现在
            # 第 34 帧；单线程（cv2.setNumThreads(1)）也无法消除 —— 说明不是线程
            # 调度，而是 OpenCV BackgroundSubtractorMOG2 内部状态在实例间不逐位
            # 可复现。200 帧自检的汇总数（599 / 76.0%）实测多次稳定，不受影响。
            # 含义：短窗口的原始条数要按 ±2 的粒度读，别把"172 vs 173"当差异。
            "cv 通道的原始检出条数在短窗口（几十帧）上有约 ±2 的运行间波动"
            "（OpenCV 背景建模内部状态所致，非本仓库代码状态泄漏；"
            "200 帧自检的汇总口径实测稳定）。跨运行的条数差异在该粒度内不算发现。",
        ],
        "groups": {},
    }
    exit_code = 0

    # ---- 静态照片组 ----
    images = resolve_images(args)
    if images:
        group: dict[str, Any] = {
            "input": str(DEFAULT_IMAGE_DIR.relative_to(ROOT)) if not args.image else "显式指定",
            "images": [str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p) for p in images],
            "backends": {},
        }
        if "cv" in wanted:
            group["backends"]["cv"] = run_cv_stills(images, cfg)
        if "world" in wanted:
            group["backends"]["world"] = run_world_stills(
                images, cfg, args.world_prompt or [], args.world_conf)

        # ★ 把"cv 在这批图上为什么是 0"算出来写进说明，避免被误读成"cv 失效"
        group.setdefault("caveats", [])
        cv_block = group["backends"].get("cv")
        if cv_block and cv_block.get("detections_total") == 0 and cv_block.get("images_total"):
            group["caveats"].append(
                "cv 通道在本组检出 0 条。CvDetector 是**固定机位背景建模**方案："
                "对单张 4K 照片做合成预热后，整幅画面与合成背景全不相同，"
                "会被并成一个巨型前景区域并被 max_area(40000 px²) 过滤掉。"
                "这是**适用范围之外**，不等于 cv 通道失效 —— "
                "它在合成视频序列里可正常检出（见 --sequence）。"
            )

        # ★ conf 这个默认值必须留下依据。没有真值就没法"标定"，
        #   但至少要能回答"它卡在分数分布的哪个位置"。
        w_block = group["backends"].get("world")
        if w_block and w_block.get("status") == EVALUATED:
            probe = w_block.get("threshold_probe") or {}
            conf = w_block.get("conf")
            group["caveats"].append(
                f"world 通道的 conf={conf} 是**演示门限，不是标定门限**："
                "独立测试集真值标注为 0，没有任何数据可用于标定阈值，"
                "把它写成'精度最优'即属编造。换门限前请先看本文件 "
                "threshold_probe（压到近零门限后的实测分数分布与逐类最高分）。"
            )
            class_top = probe.get("class_top") or {}
            unreachable = sorted(
                cls for cls, top in class_top.items()
                if conf is not None and top < float(conf)
            )
            if unreachable:
                group["caveats"].append(
                    "以下契约类别在诊断门限下有检出、但**最高分低于成品 conf="
                    f"{conf}**，在该门限下等于不可报：{', '.join(unreachable)}。"
                    "这不是模型没看到它们，是门限把它们滤掉了 —— "
                    "现场要让这几类出现，须调低 conf，并同步检查时序环节。"
                )
        payload["groups"]["still_images"] = group
    else:
        print(f"[提示] 默认图集目录不存在：{DEFAULT_IMAGE_DIR}")

    # ---- 合成序列组 ----
    if args.sequence:
        seq_images, seq_truths = build_synthetic_sequence(args.frames)
        seq: dict[str, Any] = {
            "frames": args.frames,
            "note": "同一组帧同时喂给两条通道；真值为生成器的 3 个泡沫团外接框（程序化）",
            "backends": {},
        }
        if "cv" in wanted:
            seq["backends"]["cv"] = run_cv_sequence(seq_images, seq_truths, cfg)
        if "world" in wanted:
            seq["backends"]["world"] = run_world_sequence(
                seq_images, seq_truths, cfg, args.world_prompt or [], args.world_conf)
        payload["groups"]["synthetic_sequence"] = seq

    if not payload["groups"]:
        print("没有可跑的内容。用 --image 指定图片，或加 --sequence。")
        return 2

    # ---- 写文件 ----
    output_path = Path(args.output).expanduser()
    if not output_path.is_absolute():
        output_path = ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 控制台报告 ----
    for group_name, group in payload["groups"].items():
        print(f"\n===== 对照组：{group_name} =====")
        backends = group["backends"]
        if group_name == "still_images" and backends:
            header = f"{'图':<16}" + "".join(f"{b:>26}" for b in backends)
            print(header)
            print("-" * len(header))
            for idx in range(max((len(b.get("per_image", [])) for b in backends.values()), default=0)):
                name = ""
                for b in backends.values():
                    rec = b.get("per_image", [])
                    if idx < len(rec):
                        name = rec[idx].get("name", "")
                        break
                row = f"{name:<16}"
                for b in backends.values():
                    rec = b.get("per_image", [])
                    if b.get("status") == NOT_EVALUATED:
                        row += f"{'（未评测：不可用）':>26}"
                    elif idx < len(rec):
                        r = rec[idx]
                        row += f"{r.get('count', '-'):>10} 条 {r.get('latency_ms', '-'):>9} ms"
                    else:
                        row += f"{'-':>26}"
                print(row)

        for name, block in backends.items():
            print(f"\n[{name}] 状态={block.get('status')}")
            if block.get("status") == NOT_EVALUATED:
                print(f"  不可用原因：{block.get('reason')}")
                print("  ★ 这不是「没有检到东西」，而是这一路根本没跑起来。")
                exit_code = max(exit_code, 1)
                continue
            if group_name == "still_images":
                print(f"  检出总数={block['detections_total']}  "
                      f"有检出的图={block['images_with_detection']}/{block['images_total']}  "
                      f"平均时延={block.get('latency_ms', {}).get('mean')} ms")
            else:
                print(f"  帧数={block['frames']}  检出总数={block['detections_total']}  "
                      f"有检出的帧={block['frames_with_detection']}/{block['frames']}")
                print(f"  合成真值命中={block['gt_targets_hit']}/{block['gt_targets_total']} "
                      f"({block['gt_hit_rate']:.1%} 合成口径，严禁外推)" if block.get("gt_hit_rate") is not None
                      else "  合成真值命中：无")
                print(f"  平均时延={block.get('latency_ms', {}).get('mean')} ms")
            probe = block.get("threshold_probe")
            if probe:
                if probe.get("available") is False:
                    print(f"  [诊断] 未能执行（{probe.get('reason')}）")
                else:
                    print(f"  [诊断] 门限压到 {probe['probe_conf']} 重跑 "
                          f"{probe.get('frames')} 帧：原始输出={probe.get('raw')}  "
                          f"最高分={probe.get('top_confidence')}  "
                          f"分档={probe.get('score_buckets')}")
                    if probe.get("diagnosis"):
                        print(f"         → {probe['diagnosis']}")
            for c in block.get("caveats", []):
                print(f"  [说明] {c}")
        for c in group.get("caveats", []):
            print(f"  [说明] {c}")

    print(f"\n已写入 {output_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
