#!/usr/bin/env python3
"""探海灵眸 SeaSight — OpenCV 感知评测脚本（WP-06）。

把「OpenCV 检测结果 + 时序校验 + 数据集隔离」变成可复现实验：

  1. 只认数据集清单（ml/datasets/manifests/），不扫目录 —— 防泄漏。
  2. 输出 JSON 带命令、日期、代码版本（无 .git 时用固定串 + 文件指纹）、
     样本量、分母、类别指标、confounder 分层，每项带 evidence_level。
  3. 数据集为空 / 清单未就绪 → 输出 not_evaluated（退出码 0，有明确原因），
     **绝不生成虚假精度**。
  4. 76% 抑制率口径：只针对时序链路（公式见 suppression_rate 文档），
     本脚本只在有视频/有序帧流时计算，其余情况 not_evaluated。

能力口径（冻结）：本脚本评测的是 **OpenCV 传统视觉**（edge/detector/detector.py
的 CvDetector），不是 YOLO、不是视觉大模型、不是已训练模型。
合成帧证据最高 E1-E2，禁止写成 E3/E4；真实数据默认 E3（E4 需合同凭证，本脚本不自动授予）。

退出码：
  0 = 完成（含「数据为空 → not_evaluated」这一合法状态）
  1 = 评测过程中出现运行时错误（如图像损坏）
  2 = 配置/清单错误（文件缺失、字段非法、evidence 越界）
  3 = 依赖缺失（cv2 / numpy / pyyaml），已给出补齐指引

用法：
  python ml/scripts/evaluate_opencv.py --config ml/configs/seasight.yaml --output artifacts/metrics/opencv_latest.json
  python ml/scripts/evaluate_opencv.py --config ml/configs/seasight.yaml --split all --max-frames 200
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ----------------------------------------------------------------------
# 冻结口径常量
# ----------------------------------------------------------------------

VALID_MANIFEST_TYPES = ("train", "val", "test", "blind")
VALID_STATUS = ("planned", "ready", "sealed")
VALID_DATA_TYPES = ("planned", "synthetic", "quasi_real", "real")

# evidence_level 上限（计划书 §1 与 WP-06 冻结接口）：
#   planned→E0；synthetic→E1|E2（禁止 E3/E4）；quasi_real→E2；real→E3（E4 需合同凭证）
ALLOWED_EVIDENCE: dict[str, tuple[str, ...]] = {
    "planned": ("E0",),
    "synthetic": ("E1", "E2"),
    "quasi_real": ("E2",),
    "real": ("E3",),
}

EVIDENCE_DESCRIPTIONS = {
    "E0": "规划假设（只有想法/文档/接口草案）",
    "E1": "内部实现证据（代码、单元测试、确定性仿真）",
    "E2": "受控实验（多模块软件联调，合成数据或模拟设备）",
    "E3": "真实场景（真实设备、真实海域或真实用户参与验证）",
    "E4": "规模化验证（合同、订单、回款、验收报告或复购）",
}

CODE_TAG = "seasight-wp06-20260918"  # 无 .git 仓库时的固定版本串（来源=no_git_repo）

IMG_SUFFIX = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

THIS_FILE = Path(__file__).resolve()
ML = THIS_FILE.parent.parent          # ml/
ROOT = ML.parent                      # 仓库根
EDGE_DETECTOR = ROOT / "edge" / "detector"
EDGE_SIMULATOR = ROOT / "edge" / "simulator"

NOT_EVALUATED = "not_evaluated"
EVALUATED = "evaluated"


class DependencyError(Exception):
    """依赖缺失。"""


class ConfigError(Exception):
    """配置或清单错误。"""


# ----------------------------------------------------------------------
# 纯逻辑指标函数（无第三方依赖，ml/tests 与 edge/detector/test_metrics.py 直接复用）
# ----------------------------------------------------------------------

def box_iou(a: list[float], b: list[float]) -> float:
    """两个 [x1, y1, x2, y2] 框的 IoU（纯数学，无依赖）。"""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def match_detections(
    gt_boxes: list[list[float]], det_boxes: list[list[float]], iou_threshold: float
) -> tuple[int, int, int]:
    """贪心匹配检测框与真值框，返回 (tp, fp, fn)。

    - tp：与某个未占用真值框 IoU ≥ 门限的检测
    - fp：未匹配上任何真值框的检测
    - fn：未被任何检测匹配的真值框
    """
    gt_used = [False] * len(gt_boxes)
    det_used = [False] * len(det_boxes)
    tp = 0
    for i, det in enumerate(det_boxes):
        best_j, best_iou = -1, iou_threshold
        for j, gt in enumerate(gt_boxes):
            if gt_used[j]:
                continue
            iou = box_iou(det, gt)
            if iou >= best_iou:
                best_iou, best_j = iou, j
        if best_j >= 0:
            gt_used[best_j] = True
            det_used[i] = True
            tp += 1
    fp = sum(1 for used in det_used if not used)
    fn = sum(1 for used in gt_used if not used)
    return tp, fp, fn


def compute_class_metrics(tp: int, fp: int, fn: int) -> dict[str, Any]:
    """单类 Precision / Recall / F1，带 not_evaluated 语义。

    口径（计划书 §7.1）：
      precision = tp / (tp + fp)    分母 = 预测数
      recall    = tp / (tp + fn)    分母 = 真值数
      任一分母为零 → 该指标不计算（None），不产出虚假数值。
      两个分母都为零（无预测也无真值）→ 整项 status = not_evaluated。
    """
    precision_den = tp + fp
    recall_den = tp + fn
    precision = tp / precision_den if precision_den > 0 else None
    recall = tp / recall_den if recall_den > 0 else None
    if precision is not None and recall is not None:
        # F1 = 2PR/(P+R)；P=R=0（全错且全漏）时按惯例取 0.0，而不是 None
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    else:
        f1 = None
    if precision_den == 0 and recall_den == 0:
        status, reason = NOT_EVALUATED, "无预测也无真值（分母为零），不生成虚假精度"
    elif precision is None or recall is None:
        status, reason = NOT_EVALUATED, "部分分母为零：该指标无法计算"
    else:
        status, reason = EVALUATED, ""
    return {
        "status": status,
        "reason": reason,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def suppression_rate(fed: int, absorbed: int) -> float | None:
    """时序链路抑制率（76% 口径的来源公式）。

    口径（与 edge/simulator/simulator.py TemporalValidator.stats 完全一致）：
      抑制率 = (fed - absorbed) / fed
             = 从未被稳定跟踪的检测数 / 喂进时序校验器的检测总数
    只针对**时序链路** —— 分母是「检测数」，不是「帧数」，更不是「确认事件数」。
    fed ≤ 0 → 返回 None（无输入，不得编造抑制率）。
    注意：这不是检测器本身的精度/召回指标，禁止把它写成感知精度。
    """
    if fed is None or fed <= 0:
        return None
    suppressed = max(0, fed - absorbed)
    return suppressed / fed


def resolve_evidence(data_type: str, status: str, evidence_level: str | None) -> str:
    """按冻结口径解析并校验 evidence_level。

    - status=planned → 强制 E0（没有数据就没有证据）
    - data_type 允许范围见 ALLOWED_EVIDENCE；越界直接抛 ConfigError
    """
    if status == "planned":
        return "E0"
    allowed = ALLOWED_EVIDENCE.get(data_type)
    if allowed is None:
        raise ConfigError(f"未知 data_type={data_type!r}，允许 {VALID_DATA_TYPES}")
    if evidence_level is None:
        return allowed[0]
    if evidence_level not in allowed:
        raise ConfigError(
            f"evidence_level={evidence_level!r} 超出 {data_type} 允许范围 {allowed}"
            "（冻结口径：合成帧最高 E2，不得写成 E3/E4）"
        )
    return evidence_level


def validate_manifest(data: dict[str, Any], source: str) -> dict[str, Any]:
    """校验一份清单的结构与冻结口径，返回规范化后的清单。"""
    errors: list[str] = []
    if data.get("schema_version") != "1.0":
        errors.append(f"{source}: schema_version 应为 1.0，实际 {data.get('schema_version')!r}")
    mtype = data.get("manifest_type")
    if mtype not in VALID_MANIFEST_TYPES:
        errors.append(f"{source}: manifest_type 应为 {VALID_MANIFEST_TYPES}，实际 {mtype!r}")
    status = data.get("status")
    if status not in VALID_STATUS:
        errors.append(f"{source}: status 应为 {VALID_STATUS}，实际 {status!r}")
    data_type = data.get("data_type")
    if data_type not in VALID_DATA_TYPES:
        errors.append(f"{source}: data_type 应为 {VALID_DATA_TYPES}，实际 {data_type!r}")
    iso = data.get("isolation") or {}
    if iso.get("method") not in ("physical", "logical"):
        errors.append(f"{source}: isolation.method 应为 physical|logical")
    if not str(iso.get("physical_note", "")).strip() and not str(iso.get("logical_rule", "")).strip():
        errors.append(f"{source}: isolation 必须写明 physical_note 或 logical_rule（禁止留空）")
    loc = data.get("location") or {}
    has_samples = bool(data.get("samples"))
    if not loc.get("images") and not loc.get("video") and not has_samples:
        errors.append(f"{source}: location 必须给出 images 或 video（或提供显式 samples 列表）")
    if errors:
        raise ConfigError("\n".join(errors))
    evidence = resolve_evidence(data_type, status, data.get("evidence_level"))
    out = dict(data)
    out["evidence_level"] = evidence
    return out


# ----------------------------------------------------------------------
# 依赖与配置
# ----------------------------------------------------------------------

def _check_dependencies() -> dict[str, str]:
    """检查 cv2 / numpy / pyyaml，缺失时抛出 DependencyError。

    注意：cv2 可能由 opencv-python 或 opencv-python-headless 提供，
    两种发行包都被接受（本脚本是服务端评测，推荐 headless）。
    """
    versions: dict[str, str] = {}
    missing: list[str] = []
    try:
        import cv2  # type: ignore[import-not-found]

        versions["cv2"] = cv2.__version__
    except ImportError:
        missing.append("opencv-python-headless（或 opencv-python），提供 cv2")
    try:
        import numpy  # type: ignore[import-not-found]

        versions["numpy"] = numpy.__version__
    except ImportError:
        missing.append("numpy")
    try:
        import yaml  # type: ignore[import-not-found]

        versions["pyyaml"] = yaml.__version__
    except ImportError:
        missing.append("pyyaml")
    if missing:
        raise DependencyError(missing)
    return versions


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml  # type: ignore[import-not-found]

    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"YAML 解析失败：{path} —— {exc}") from exc


def _load_config(path: Path) -> tuple[dict[str, Any], Path]:
    """读取数据集配置（seasight.yaml），返回 (归一化配置, 配置文件绝对路径)。"""
    if not path.exists():
        raise ConfigError(f"配置文件不存在：{path}")
    raw = _load_yaml(path)
    names = raw.get("names") or []
    if isinstance(names, dict):
        names = [names[k] for k in sorted(names)]
    if not names:
        raise ConfigError(f"{path}: 缺少 names（类别定义）")
    eval_cfg = raw.get("evaluation") or {}
    config = {
        "path": raw.get("path", "."),
        "names": list(names),
        "evaluation": eval_cfg,
    }
    return config, path.resolve()


def _load_detector() -> Any:
    """延迟导入 CvDetector（edge/detector/detector.py）。

    用 DEFAULT_CONFIG（与 edge/config.yaml 逐项对账，见 test_detector.py 契约守卫），
    ROI 为空 = 全画面。
    """
    sys.path.insert(0, str(EDGE_DETECTOR))
    from detector import CvDetector  # type: ignore[import-not-found]

    return CvDetector()


def _load_temporal_validator(temporal_cfg: dict[str, Any]) -> Any:
    """延迟导入 TemporalValidator（edge/simulator/simulator.py），参数与 edge/config.yaml 对齐。"""
    sys.path.insert(0, str(EDGE_SIMULATOR))
    from simulator import TemporalValidator  # type: ignore[import-not-found]

    return TemporalValidator(
        window_frames=int(temporal_cfg.get("window_frames", 15)),
        min_hits=int(temporal_cfg.get("min_hits", 3)),
        grid_size=int(temporal_cfg.get("grid_size", 64)),
        min_confidence=float(temporal_cfg.get("min_confidence", 0.45)),
        match_distance=float(temporal_cfg.get("match_distance", 60.0)),
        max_misses=int(temporal_cfg.get("max_misses", 5)),
        min_iou=float(temporal_cfg.get("min_iou", 0.25)),
        decimation=int(temporal_cfg.get("decimation", 4)),
    )


# ----------------------------------------------------------------------
# 数据集清单解析
# ----------------------------------------------------------------------

def discover_manifests(manifest_dir: Path) -> dict[str, dict[str, Any]]:
    """扫描清单目录，按 manifest_type 分组；同类型重复直接报错。"""
    if not manifest_dir.exists():
        return {}
    found: dict[str, dict[str, Any]] = {}
    manifest_files = sorted(manifest_dir.glob("*.yaml")) + sorted(manifest_dir.glob("*.yml"))
    for p in manifest_files:
        if p.name.startswith("template"):
            continue   # 模板是空白表单，不是真实清单
        data = validate_manifest(_load_yaml(p), p.name)
        mtype = data["manifest_type"]
        if mtype in found:
            raise ConfigError(f"清单目录 {manifest_dir} 中存在重复的 {mtype} 类型：{p.name}")
        found[mtype] = data
    return found


def _root() -> Path:
    """仓库根目录（ml/scripts/evaluate_opencv.py → ml/ → 根）。"""
    return ROOT


def resolve_samples(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """解析出 (path, conditions, objects) 样本列表；目录不存在/为空 → 空列表。

    样本来源二选一：
      - 清单显式 samples 列表（路径相对仓库根解析）
      - location.images 目录自动发现（相对仓库根解析）
    """
    location = manifest.get("location") or {}
    samples: list[dict[str, Any]] = []
    explicit = manifest.get("samples")
    if explicit:
        for s in explicit:
            p = Path(s["path"])
            if not p.is_absolute():
                p = (_root() / p).resolve()
            samples.append(
                {
                    "path": p,
                    "conditions": dict(s.get("conditions") or {}),
                    "objects": s.get("objects"),
                }
            )
        return samples

    rel = location.get("images")
    if not rel:
        return []
    img_root = (_root() / rel).resolve()
    if not img_root.exists():
        return []
    default_conditions = dict(manifest.get("conditions") or {})
    for p in sorted(img_root.rglob("*")):
        if p.is_file() and p.suffix.lower() in IMG_SUFFIX:
            samples.append({"path": p, "conditions": dict(default_conditions), "objects": None})
    return samples


def _label_path_for(img: Path, manifest: dict[str, Any]) -> Path | None:
    """由图片路径推导 YOLO 标签路径（images/ → labels/ 惯例）。"""
    labels_rel = (manifest.get("location") or {}).get("labels")
    images_rel = (manifest.get("location") or {}).get("images")
    if not labels_rel or not images_rel:
        return None
    labels_root = (_root() / labels_rel).resolve()
    images_root = (_root() / images_rel).resolve()
    try:
        rel = img.relative_to(images_root)
    except ValueError:
        return None
    parts = list(rel.parts)
    for i, seg in enumerate(parts):
        if seg == "images":
            parts[i] = "labels"
            break
    parts[-1] = Path(parts[-1]).stem + ".txt"
    candidate = labels_root.joinpath(*parts)
    return candidate if candidate.exists() else None


def load_yolo_labels(txt: Path, w: int, h: int, num_classes: int) -> list[dict[str, Any]]:
    """解析 YOLO 标签：class cx cy bw bh（归一化）→ 像素 bbox。

    返回的 class 是类别索引，调用方用 names[idx] 转成类别名。
    """
    boxes: list[dict[str, Any]] = []
    try:
        lines = txt.read_text(encoding="utf-8").splitlines()
    except OSError:
        return boxes
    for line in lines:
        parts = line.split()
        if len(parts) != 5:
            continue
        try:
            cid, cx, cy, bw, bh = (float(p) for p in parts)
        except ValueError:
            continue
        if not (0 <= int(cid) < num_classes):
            continue
        x1 = (cx - bw / 2.0) * w
        y1 = (cy - bh / 2.0) * h
        x2 = (cx + bw / 2.0) * w
        y2 = (cy + bh / 2.0) * h
        boxes.append({"class": int(cid), "bbox": [x1, y1, x2, y2]})
    return boxes


# ----------------------------------------------------------------------
# 评测核心
# ----------------------------------------------------------------------

def _accumulate(counters: dict[str, Any], cls: str, tp: int, fp: int, fn: int) -> None:
    c = counters.setdefault(cls, {"tp": 0, "fp": 0, "fn": 0})
    c["tp"] += tp
    c["fp"] += fp
    c["fn"] += fn


def evaluate_split(
    manifest: dict[str, Any],
    names: list[str],
    iou_threshold: float,
    max_frames: int,
    temporal_cfg: dict[str, Any],
    gt_map: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """评测单个分集（独立测试集等）。返回结构化结果，空数据 → not_evaluated。

    gt_map：WP-13 数据协议 GT（{文件名: {"objects": [...]}}）；提供时地面真值
    只取协议 GT（样本不在协议中则跳过），证据纪律由协议校验保障。
    """
    result: dict[str, Any] = {
        "status": NOT_EVALUATED,
        "reason": "",
        "evidence_level": manifest["evidence_level"],
        "sample_size": 0,
        "denominators": {},
        "metrics": {},
        "confounders": {},
        "temporal_pipeline": {},
        "gt_source": "protocol_json" if gt_map is not None else "manifest",
    }

    if manifest.get("status") == "planned":
        result["reason"] = "清单未就绪（status=planned）：未采集或未放行数据，禁止评测"
        return result

    samples = resolve_samples(manifest)
    if not samples:
        result["reason"] = "空数据集：未解析到任何图片（目录不存在或为空）"
        return result
    if gt_map is not None:
        before = len(samples)
        samples = [s for s in samples if s["path"].name in gt_map]
        result["skipped_from_gt_json"] = before - len(samples)
        if not samples:
            result["reason"] = f"空数据集：无任何样本命中数据协议 GT（共 {before} 个样本全部跳过）"
            return result

    detector = _load_detector()
    import cv2  # type: ignore[import-not-found]

    counters: dict[str, dict[str, int]] = {}
    strata: dict[str, dict[str, dict[str, dict[str, int]]]] = {}
    processed = 0
    skipped = 0
    confounders = manifest.get("confounders") or []
    total_gt: dict[str, int] = {}
    total_det: dict[str, int] = {}

    for sample in samples:
        if max_frames and processed >= max_frames:
            break
        img = cv2.imread(str(sample["path"]))
        if img is None:
            skipped += 1
            continue
        processed += 1
        h, w = img.shape[:2]

        dets = detector.detect(img)
        det_by_class: dict[str, list[list[float]]] = {c: [] for c in names}
        for d in dets:
            cls = d.get("class")
            if cls in det_by_class:
                det_by_class[cls].append([float(v) for v in d["bbox"]])

        gt_by_class: dict[str, list[list[float]]] = {c: [] for c in names}
        if gt_map is not None:
            entry = gt_map.get(sample["path"].name)
            for obj in (entry or {}).get("objects", []):
                cls = obj.get("class")
                if cls in gt_by_class:
                    gt_by_class[cls].append([float(v) for v in obj["bbox"]])
        elif sample.get("objects"):
            for obj in sample["objects"]:
                cls = obj.get("class")
                if cls in gt_by_class:
                    gt_by_class[cls].append([float(v) for v in obj["bbox"]])
        else:
            lbl = _label_path_for(sample["path"], manifest)
            if lbl is not None:
                for box in load_yolo_labels(lbl, w, h, len(names)):
                    gt_by_class[names[box["class"]]].append(box["bbox"])

        for cls in names:
            tp, fp, fn = match_detections(gt_by_class[cls], det_by_class[cls], iou_threshold)
            _accumulate(counters, cls, tp, fp, fn)
            total_gt[cls] = total_gt.get(cls, 0) + len(gt_by_class[cls])
            total_det[cls] = total_det.get(cls, 0) + len(det_by_class[cls])
            for conf in confounders:
                value = (sample.get("conditions") or {}).get(conf)
                if value is None:
                    continue
                bucket = strata.setdefault(conf, {}).setdefault(str(value), {})
                _accumulate(bucket, cls, tp, fp, fn)

    if processed == 0:
        result["reason"] = f"空数据集：{skipped} 个样本均无法读取"
        return result

    result["sample_size"] = processed
    result["denominators"] = {
        cls: {"gt_boxes": total_gt.get(cls, 0), "detections": total_det.get(cls, 0)}
        for cls in names
    }
    for cls in names:
        c = counters.get(cls, {"tp": 0, "fp": 0, "fn": 0})
        m = compute_class_metrics(c["tp"], c["fp"], c["fn"])
        m["evidence_level"] = manifest["evidence_level"]
        result["metrics"][cls] = m

    # 整体状态：至少一个类别有有效分母才算 evaluated，否则 not_evaluated
    any_evaluated = any(
        result["metrics"][cls]["status"] == EVALUATED for cls in names
    )
    if any_evaluated:
        result["status"] = EVALUATED
    else:
        result["reason"] = "所有类别均无有效分母（无真值或无预测），无法计算精度/召回"

    # confounder 分层：只有样本带元数据才产出分层，否则 not_evaluated
    if strata:
        for conf, values in strata.items():
            result["confounders"][conf] = {}
            for value, cls_counters in values.items():
                result["confounders"][conf][value] = {}
                for cls in names:
                    c = cls_counters.get(cls, {"tp": 0, "fp": 0, "fn": 0})
                    m = compute_class_metrics(c["tp"], c["fp"], c["fn"])
                    m["evidence_level"] = manifest["evidence_level"]
                    result["confounders"][conf][value][cls] = m
    else:
        result["confounders"] = {
            "status": NOT_EVALUATED,
            "reason": "样本缺少 confounder 元数据（conditions），无法分层",
        }

    result["temporal_pipeline"] = _evaluate_temporal(manifest, names, temporal_cfg)
    return result


def _evaluate_temporal(
    manifest: dict[str, Any], names: list[str], temporal_cfg: dict[str, Any]
) -> dict[str, Any]:
    """时序链路抑制率（只针对时序链路）。

    需要有序帧来源（视频文件）。口径与 simulator.TemporalValidator 一致：
      抑制率 = (fed - absorbed) / fed，分母是检测数。
    没有视频/有序帧 → not_evaluated；绝不编造抑制率。
    """
    out: dict[str, Any] = {
        "status": NOT_EVALUATED,
        "reason": "",
        "evidence_level": manifest["evidence_level"],
        "scope": "temporal_link_only",
        "formula": "suppression_rate = (fed - absorbed) / fed（检测数口径）",
        "note": "抑制率只针对时序链路，不是检测器精度，禁止外推为感知精度",
    }
    video_rel = (manifest.get("location") or {}).get("video")
    if not video_rel:
        out["reason"] = "清单未提供视频/有序帧流（location.video 为空），无法计算时序链路抑制率"
        return out

    video_path = (_root() / video_rel).resolve()
    if not video_path.exists():
        out["reason"] = f"视频文件不存在：{video_path}"
        return out

    import cv2  # type: ignore[import-not-found]

    detector = _load_detector()
    validator = _load_temporal_validator(temporal_cfg)
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        out["reason"] = f"无法打开视频：{video_path}"
        return out

    frames = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames += 1
        validator.push(detector.detect(frame))
        validator.sweep(validator.decimation)
    cap.release()

    st = validator.stats
    rate = suppression_rate(st["fed"], st["absorbed"])
    out["status"] = EVALUATED if rate is not None else NOT_EVALUATED
    out["frames"] = frames
    out["fed"] = st["fed"]
    out["absorbed"] = st["absorbed"]
    out["suppressed"] = st["suppressed"]
    out["confirmed"] = st["confirmed"]
    out["suppression_rate"] = rate
    out["parameters"] = {
        "window_frames": validator.window_frames,
        "min_hits": validator.min_hits,
        "min_confidence": validator.min_confidence,
        "decimation": validator.decimation,
        "match_distance": validator.match_distance,
        "min_iou": validator.min_iou,
        "max_misses": validator.max_misses,
    }
    if rate is None:
        out["reason"] = "时序校验器未收到任何检测（fed=0），无法计算抑制率"
    return out


# ----------------------------------------------------------------------
# 报告组装
# ----------------------------------------------------------------------

def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fingerprint(files: list[Path]) -> str:
    h = hashlib.sha256()
    for f in files:
        h.update(str(f).encode("utf-8"))
        try:
            h.update(f.read_bytes())
        except OSError:
            pass
    return h.hexdigest()


def _git_rev() -> str | None:
    """用 pathlib 直接读 .git/HEAD，不依赖 git 可执行文件（无子进程）。"""
    git_dir = ROOT / ".git"
    if not git_dir.exists():
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref: "):
            ref_path = git_dir / head[5:].strip()
            if ref_path.exists():
                return ref_path.read_text(encoding="utf-8").strip()[:12]
            return head[5:].strip()
        return head[:12]
    except OSError:
        return None


def _code_version(config_path: Path, manifest_dir: Path) -> dict[str, Any]:
    """代码版本：有 .git 取 HEAD 短哈希；无 .git 用固定串 + 关键文件指纹（来源注明）。"""
    rev = _git_rev()
    if rev:
        source = "git"
        value = rev
    else:
        source = "no_git_repo"
        value = CODE_TAG
    files = [THIS_FILE, EDGE_DETECTOR / "detector.py", config_path]
    if manifest_dir.exists():
        files += sorted(manifest_dir.glob("*.yaml")) + sorted(manifest_dir.glob("*.yml"))
    return {
        "source": source,
        "value": value,
        "fingerprint": _fingerprint(files),
        "note": "有 .git 时取 HEAD 短哈希；无 .git 时 value 为固定版本串，fingerprint 覆盖评测脚本/检测器/配置/清单",
    }


def _load_protocol_gt(path: Path) -> tuple[dict[str, dict[str, Any]] | None, str]:
    """加载并校验 WP-13 数据协议 JSON（SeaSight COCO-like），返回 GT 映射。

    返回 (gt_map, reason)：gt_map = {图片文件名: {"objects": [{class, bbox}]}}，
    bbox 已由 COCO xywh 转为 [x1, y1, x2, y2]；类别按 categories.id→name 映射。
    校验核心复用 convert_annotations.validate_dataset（与 check_dataset.py 同一
    懒加载模式）：无效 / 越级证据 / 跨文件泄漏 → (None, reason)，调用方输出
    not_evaluated，绝不生成虚假精度。
    """
    import importlib.util

    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "seasight_convert_annotations", here / "convert_annotations.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    try:
        data, _ = mod.load_json(str(path))
    except mod.ConfigError as exc:
        return None, f"数据协议 JSON 无法解析：{exc}"

    rep = mod.validate_dataset(
        data, source_path=str(path), check_files=False, check_checksums=False
    )
    if rep["status"] == "invalid":
        problems = rep.get("problems", [])
        return None, (
            f"数据协议校验未通过（{len(problems)} 个阻断问题）："
            + "; ".join(str(p) for p in problems[:3])
        )
    if rep.get("isolation_violation"):
        return None, "数据协议存在跨文件泄漏（isolation_violation=True），禁止评测"

    cat_name = {c["id"]: c["name"] for c in data.get("categories", [])}
    by_image: dict[int, dict[str, Any]] = {}
    for img in data.get("images", []):
        by_image[img["id"]] = {"file_name": img.get("file_name", ""), "objects": []}
    for ann in data.get("annotations", []):
        img = by_image.get(ann.get("image_id"))
        if img is None:
            continue
        cat = cat_name.get(ann.get("category_id"))
        if cat is None:
            continue
        x, y, w, h = (float(v) for v in (ann.get("bbox") or [0, 0, 0, 0])[:4])
        img["objects"].append({"class": cat, "bbox": [x, y, x + w, y + h]})
    gt_map: dict[str, dict[str, Any]] = {}
    for img in by_image.values():
        name = img["file_name"]
        if name:
            gt_map[Path(name).name] = {"objects": img["objects"]}
    return gt_map, ""


def build_report(
    config: dict[str, Any],
    config_path: Path,
    manifest_dir: Path,
    split: str,
    iou_threshold: float,
    max_frames: int,
    command: str,
    deps: dict[str, str],
    manifests: dict[str, dict[str, Any]],
    gt_json: Path | None = None,
) -> dict[str, Any]:
    """组装完整评测报告 JSON。"""
    names = config["names"]
    eval_cfg = config["evaluation"]
    temporal_cfg = eval_cfg.get("temporal") or {}

    report: dict[str, Any] = {
        "schema_version": "1.0",
        "status": NOT_EVALUATED,
        "not_evaluated_reasons": [],
        "command": command,
        "date": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "code_version": _code_version(config_path, manifest_dir),
        "config": {
            "path": str(config_path),
            "hash": _sha256_text(config_path.read_text(encoding="utf-8")),
            "names": names,
        },
        "dependencies": deps,
        "dataset": {
            "manifest_dir": str(manifest_dir),
            "split": split,
            "manifest_present": split in manifests,
            "empty": True,
            "sample_size": 0,
            "data_type": None,
            "evidence_level": None,
            "isolation": None,
        },
        "denominators": {},
        "metrics": {"per_class": {}, "temporal_pipeline": {}},
        "confounders": {},
    }

    if not manifests:
        report["not_evaluated_reasons"].append(
            f"未找到任何数据集清单（{manifest_dir} 不存在或为空）"
        )
        return report

    if split not in manifests:
        report["not_evaluated_reasons"].append(
            f"缺少 {split} 分集清单（可用: {sorted(manifests)}）"
        )
        return report

    manifest = manifests[split]
    report["dataset"].update(
        {
            "manifest_name": manifest.get("name"),
            "data_type": manifest.get("data_type"),
            "evidence_level": manifest["evidence_level"],
            "status": manifest.get("status"),
            "isolation": manifest.get("isolation"),
        }
    )

    # WP-13：数据协议 GT（--gt-json）—— 校验失败/越级证据 → not_evaluated
    gt_map: dict[str, dict[str, Any]] | None = None
    if gt_json is not None:
        gt_map, gt_reason = _load_protocol_gt(gt_json)
        if gt_map is None:
            report["not_evaluated_reasons"].append(f"数据协议 GT 不可用：{gt_reason}")
            return report
        report["dataset"]["gt_source"] = "protocol_json"
        report["dataset"]["gt_json"] = str(gt_json)

    split_result = evaluate_split(
        manifest, names, iou_threshold, max_frames, temporal_cfg, gt_map=gt_map
    )
    report["dataset"]["sample_size"] = split_result["sample_size"]
    report["dataset"]["empty"] = split_result["sample_size"] == 0
    report["denominators"] = split_result["denominators"]
    report["metrics"]["per_class"] = split_result["metrics"]
    report["metrics"]["temporal_pipeline"] = split_result["temporal_pipeline"]
    report["confounders"] = split_result["confounders"]

    if split_result["status"] == NOT_EVALUATED:
        report["status"] = NOT_EVALUATED
        report["not_evaluated_reasons"].append(split_result["reason"])
    else:
        report["status"] = EVALUATED

    # 抑制率口径说明：只针对时序链路
    tp = report["metrics"]["temporal_pipeline"]
    if tp.get("status") == EVALUATED:
        report["not_evaluated_reasons"].append(
            "时序链路抑制率已计算；口径=时序链路（检测数分母），不代表检测器精度"
        )
    return report


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="探海灵眸 SeaSight — OpenCV 感知评测（WP-06）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "退出码：0=完成（含空数据 not_evaluated）；1=运行时错误；2=配置/清单错误；3=依赖缺失\n"
            "示例：\n"
            "  python ml/scripts/evaluate_opencv.py --config ml/configs/seasight.yaml \\\n"
            "      --output artifacts/metrics/opencv_latest.json\n"
            "  python ml/scripts/evaluate_opencv.py --split all --max-frames 200\n"
        ),
    )
    parser.add_argument("--config", default="ml/configs/seasight.yaml", help="数据集/评测配置")
    parser.add_argument("--output", default="artifacts/metrics/opencv_latest.json", help="输出 JSON 路径")
    parser.add_argument("--manifest-dir", default=None, help="覆盖清单目录")
    parser.add_argument(
        "--split",
        default="test",
        choices=list(VALID_MANIFEST_TYPES) + ["all"],
        help="评测分集（默认 test=独立测试集，评审口径）",
    )
    parser.add_argument("--iou-threshold", type=float, default=None, help="匹配 IoU 门限（默认取配置）")
    parser.add_argument("--max-frames", type=int, default=0, help="每个分集最多处理帧数（0=不限）")
    parser.add_argument(
        "--gt-json",
        default=None,
        metavar="DATASET_JSON",
        help="WP-13 数据协议 JSON（SeaSight COCO-like）：提供时地面真值取自协议 JSON "
             "（经协议校验：跳过坏样本、拒绝越级证据、检测泄漏），而非 manifest 标签；"
             "协议无效/越级证据/空数据 → not_evaluated，绝不生成虚假精度",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # 重建可复现命令行（含脚本路径）：python ml/scripts/evaluate_opencv.py <args>
    command = "python " + " ".join(sys.argv)

    try:
        deps = _check_dependencies()
    except DependencyError as exc:
        missing = "\n  - ".join(exc.args[0])
        print(
            "[依赖缺失] 评测无法进行，已阻止产出任何指标（绝不伪造精度）。\n"
            f"  缺失：\n  - {missing}\n"
            "  补齐指引（固定版本）：\n"
            "    .\\.venv-analysis\\Scripts\\python.exe -m pip install opencv-python-headless==4.10.0.84 numpy==1.26.4 pyyaml==6.0.3\n"
            "  安装后重新运行本脚本。",
            file=sys.stderr,
        )
        return 3

    try:
        config, config_path = _load_config(Path(args.config))
        eval_cfg = config["evaluation"]
        manifest_dir = Path(args.manifest_dir) if args.manifest_dir else (
            config_path.parent / eval_cfg.get("manifest_dir", "../datasets/manifests")
        )
        manifest_dir = manifest_dir.resolve()
        iou_threshold = float(args.iou_threshold if args.iou_threshold is not None
                              else eval_cfg.get("iou_threshold", 0.5))
        max_frames = int(args.max_frames)

        manifests = discover_manifests(manifest_dir)
        gt_json = Path(args.gt_json) if args.gt_json else None

        if args.split == "all":
            per_split: dict[str, dict[str, Any]] = {}
            for sp in VALID_MANIFEST_TYPES:
                per_split[sp] = build_report(
                    config, config_path, manifest_dir, sp,
                    iou_threshold, max_frames, command, deps, manifests,
                    gt_json=gt_json,
                )
            report = dict(per_split["test"])   # 整体状态以独立测试集为准
            report["splits"] = per_split
        else:
            report = build_report(
                config, config_path, manifest_dir, args.split,
                iou_threshold, max_frames, command, deps, manifests,
                gt_json=gt_json,
            )
    except ConfigError as exc:
        print(f"[配置/清单错误] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 —— 运行期错误（图像损坏等）
        print(f"[运行时错误] {exc}", file=sys.stderr)
        return 1

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[评测] 状态={report['status']} 分集={args.split} 输出={out_path}")
    if report["status"] == NOT_EVALUATED:
        for reason in report["not_evaluated_reasons"]:
            print(f"  · not_evaluated：{reason}")
    else:
        for cls, m in report["metrics"]["per_class"].items():
            print(f"  · {cls}: precision={m['precision']} recall={m['recall']} f1={m['f1']} (n_gt={report['denominators'][cls]['gt_boxes']}, n_det={report['denominators'][cls]['detections']})")
        tp = report["metrics"]["temporal_pipeline"]
        if tp.get("status") == EVALUATED:
            print(f"  · 时序链路抑制率={tp['suppression_rate']:.1%}（fed={tp['fed']}, absorbed={tp['absorbed']}, confirmed={tp['confirmed']}）")
    print(f"[评测] 样本量={report['dataset']['sample_size']} 证据等级={report['dataset'].get('evidence_level')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
