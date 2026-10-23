"""留出集评估与阈值扫描（部署门禁的依据）。

两个用途：

1. ``evaluate`` —— 在留出集上量出 mAP50 / P / R / 每类指标，作为
   **部署门禁**：不达标不许切换权重（作业台的部署接口据此拦人）。
2. ``sweep_threshold`` —— 「免训练校准」档的实现：固定模型，在留出集上
   扫若干置信度阈值，选 F1 最高者写进 ``models/active/tuning.json``。

指标口径（对外表述必须一致）：这里报的是**留出集精度**，不是现场精度；
现场部署的是受控实验环境，对外仍按 E2/E3 证据分级表达。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from .dataset import Dataset

logger = logging.getLogger("oceanus.grasp.evaluate")

ProgressCb = Callable[[str, dict[str, Any]], None]


class EvaluationError(RuntimeError):
    """评估不可用（缺 ultralytics / 模型 / 数据集）。"""


def _require_ultralytics() -> Any:
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise EvaluationError(
            "评估需要 ultralytics：请在训练机或本机 `pip install ultralytics`。"
        ) from exc
    return YOLO


def _resolve_model(cfg: dict[str, Any], model_path: str | Path | None) -> Path:
    if model_path:
        path = Path(model_path)
        if not path.exists():
            raise EvaluationError(f"模型不存在: {path}")
        return path
    active = Path(__file__).resolve().parent.parent / "models" / "active"
    for name in ("best.onnx", "best.pt"):
        if (active / name).exists():
            return active / name
    raise EvaluationError(f"未指定模型，且 active 目录（{active}）里没有 best.onnx / best.pt")


def _resolve_data_yaml(cfg: dict[str, Any], dataset_yaml: str | Path | None, rebuild: bool) -> Path:
    if dataset_yaml:
        path = Path(dataset_yaml)
        if not path.exists():
            raise EvaluationError(f"数据集描述不存在: {path}")
        return path
    dataset = Dataset.from_config(cfg)
    built = dataset.built_dir / "data.yaml"
    if rebuild or not built.exists():
        dataset.build()
    return built


def evaluate(
    cfg: dict[str, Any],
    *,
    model_path: str | Path | None = None,
    dataset_yaml: str | Path | None = None,
    split: str = "val",
    progress_cb: ProgressCb | None = None,
) -> dict[str, Any]:
    """在留出集上评估，返回指标字典。"""
    YOLO = _require_ultralytics()
    model_path = _resolve_model(cfg, model_path)
    yaml_path = _resolve_data_yaml(cfg, dataset_yaml, rebuild=False)
    vision = cfg.get("vision", {}) or {}
    imgsz = int(vision.get("input_size", 320))

    if progress_cb:
        progress_cb("eval", {"stage": "eval", "model": str(model_path)})

    model = YOLO(str(model_path))
    # ★ workers=0 必须显式传：ultralytics val 默认开多进程 DataLoader（num_workers=8）。
    #   Windows 上走 spawn 而不是 fork，从「已在运行的服务进程」或「没加
    #   __main__ 守卫的脚本」里调用时会永远卡在子进程引导阶段 —— 实测表现为
    #   /api/evaluate 一直不返回，服务日志里连访问行都不打印。
    #   树莓派上没有 spawn 问题，但评测集只有几十~几百张，单进程足够快，
    #   不值得为这点吞吐引入多进程。
    #   plots=False：作业台是无人界面，画图纯属浪费；而且数据集声明 2 类、
    #   模型只有 1 类时，ultralytics 的画图代码会在 names[1] 上抛 KeyError: 1
    #   （实测把「一键场景校准」整个跑挂）。评估只要指标，不要图。
    results = model.val(data=str(yaml_path), split=split, imgsz=imgsz,
                        verbose=False, plots=False, workers=0)
    box = getattr(results, "box", None)
    if box is None:
        raise EvaluationError("评估未返回检测指标（检查数据集类别是否与模型一致）")

    names = getattr(results, "names", {}) or {}
    per_class: dict[str, dict[str, float]] = {}
    # ★ 不能写 `getattr(box, "maps", []) or []`：maps 是 numpy 数组，`array or []`
    #   会交给 numpy 判真值，元素多于一个就抛 "truth value of an array ... is
    #   ambiguous"。单类模型恰好只有一个元素（size-1 数组可判真值）所以从没暴露；
    #   换成 2 类（塑料袋子 + 浮标负类）后必炸 —— 训练全跑完、ONNX 也导出了，
    #   却在最后一步评估崩掉，是很贵的失败。显式判 None 才是对的。
    maps_attr = getattr(box, "maps", None)
    maps = [float(v) for v in maps_attr] if maps_attr is not None else []
    ap_attr = getattr(box, "ap_class_index", None)
    ap_index = list(ap_attr) if ap_attr is not None else []
    for slot, cls_idx in enumerate(ap_index):
        name = names.get(int(cls_idx), str(cls_idx))
        per_class[name] = {
            "map50_95": round(float(maps[slot]), 4) if slot < len(maps) else 0.0,
        }

    metrics = {
        "model": str(model_path),
        "split": split,
        "map50": round(float(box.map50), 4),
        "map50_95": round(float(box.map), 4),
        "precision": round(float(box.mp), 4),
        "recall": round(float(box.mr), 4),
        "per_class": per_class,
    }
    logger.info("评估完成 map50=%.4f p=%.4f r=%.4f", metrics["map50"],
                metrics["precision"], metrics["recall"])
    if progress_cb:
        progress_cb("done", {"stage": "eval", **metrics})
    return metrics


def passes(metrics: dict[str, Any], min_map50: float, min_recall: float = 0.0,
           min_precision: float = 0.0) -> tuple[bool, str]:
    """部署门禁判断，返回 (是否通过, 原因)。

    ★ 这是**唯一**的门禁实现：CLI（``finetune_pool.py``）、控制台训练后的评估、
    控制台部署，三处都调它。精确率是机械臂场景的主指标（误抓代价高），
    口径若在三处各写一遍，漂移一次就是「门禁说通过、其实不该抓」。

    ``min_precision`` 默认 0.0（不卡）：既有调用点的行为与测试口径都不变，
    想要精确率门禁的地方显式传值 —— 不做静默决策。
    """
    map50 = float(metrics.get("map50") or 0.0)
    recall = float(metrics.get("recall") or 0.0)
    precision = float(metrics.get("precision") or 0.0)
    if map50 < min_map50:
        return False, f"mAP50 {map50:.3f} 低于门禁 {min_map50:.3f}"
    if recall < min_recall:
        return False, f"召回 {recall:.3f} 低于门禁 {min_recall:.3f}"
    if precision < min_precision:
        return False, (f"精确率 {precision:.3f} 低于门禁 {min_precision:.3f}"
                       f"（机械臂误抓代价高，精确率是主指标）")
    return True, f"mAP50 {map50:.3f} ≥ 门禁 {min_map50:.3f}"


def roi_from_labels(built_dir: str | Path, pad: float = 0.05) -> list[float] | None:
    """从已标注的框推一个场景 ROI（归一化 ``x, y, w, h``）。

    为什么免训练校准要顺带写 ROI：现场再训练的目标就是「把模型特化到当前场景」。
    所有标注框的并集，就是「目标真正会出现的区域」；把它当 ROI 之后，落在区域外
    的检出在 Detector 层就进不了抓取候选 —— 等于用一次标注把背景误检物理排除掉。
    在固定背景 + 固定光照 + 固定物体位置的演示场景里，这比单纯调置信度阈值有效
    得多（README 的误检归因：86.6% 的误检落在纯背景上）。

    没有任何标注框时返回 None（不写 ROI），避免凭空收紧候选区。
    """
    x1 = y1 = float("inf")
    x2 = y2 = float("-inf")
    found = False
    for split in ("train", "val"):
        for lbl in sorted((Path(built_dir) / "labels" / split).glob("*.txt")):
            for line in lbl.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) != 5:
                    continue
                _, cx, cy, w, h = (float(v) for v in parts)
                x1 = min(x1, cx - w / 2)
                y1 = min(y1, cy - h / 2)
                x2 = max(x2, cx + w / 2)
                y2 = max(y2, cy + h / 2)
                found = True
    if not found:
        return None
    left = max(0.0, x1 - pad)
    top = max(0.0, y1 - pad)
    right = min(1.0, x2 + pad)
    bottom = min(1.0, y2 + pad)
    if right <= left or bottom <= top:
        return None
    return [round(left, 4), round(top, 4), round(right - left, 4), round(bottom - top, 4)]


def sweep_threshold(
    cfg: dict[str, Any],
    *,
    dataset_yaml: str | Path | None = None,
    thresholds: tuple[float, ...] = (0.30, 0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80),
    progress_cb: ProgressCb | None = None,
) -> dict[str, Any]:
    """免训练校准：固定模型扫阈值取 F1 最高者，并把「标注框并集」写成 ROI，落进 tuning.json。"""
    YOLO = _require_ultralytics()
    model_path = _resolve_model(cfg, None)
    yaml_path = _resolve_data_yaml(cfg, dataset_yaml, rebuild=True)
    vision = cfg.get("vision", {}) or {}
    imgsz = int(vision.get("input_size", 320))
    model = YOLO(str(model_path))

    rows: list[dict[str, float]] = []
    for thr in thresholds:
        # 同 evaluate()：单进程 + 不画图（画图在"数据集 2 类、模型 1 类"时会 KeyError）
        res = model.val(data=str(yaml_path), imgsz=imgsz, conf=thr,
                        verbose=False, plots=False, workers=0)
        box = getattr(res, "box", None)
        if box is None:
            continue
        p, r = float(box.mp), float(box.mr)
        f1 = (2 * p * r / (p + r)) if (p + r) > 0 else 0.0
        rows.append({"conf": float(thr), "precision": round(p, 4),
                     "recall": round(r, 4), "f1": round(f1, 4)})
        if progress_cb:
            progress_cb("sweep", {"stage": "sweep", **rows[-1]})

    if not rows:
        raise EvaluationError("阈值扫描未获得任何指标")

    best = max(rows, key=lambda x: x["f1"])
    # 顺带把「标注框并集」写成 ROI：零成本拿到一层场景约束（见 roi_from_labels）
    roi = roi_from_labels(Path(yaml_path).parent)
    tuning = {
        "conf_threshold": best["conf"],
        "iou_threshold": float(vision.get("iou_threshold", 0.45)),
        "roi": roi,
        "sweep": rows,
        "chosen_by": "max_f1_on_val_split",
    }
    active = model_path.parent if model_path.parent.name == "active" else Path(
        __file__).resolve().parent.parent / "models" / "active"
    active.mkdir(parents=True, exist_ok=True)
    (active / "tuning.json").write_text(
        json.dumps(tuning, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("免训练校准完成：conf=%.2f f1=%.4f", best["conf"], best["f1"])
    return {"tuning": tuning, "best": best, "sweep": rows,
            "tuning_path": str(active / "tuning.json")}
