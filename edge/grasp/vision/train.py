"""三档训练（应对树莓派算力现实的阶梯）。

| 档位 | 做法 | 量级 | 用途 |
| --- | --- | --- | --- |
| ``calibrate`` | 不训练，只扫置信度阈值 + 写 ROI/阈值调参 | 秒级 | 演示日兜底保险 |
| ``local``     | 冻结骨干只训检测头（Pi 上跑） | Pi4 约 30–60 分钟 | 标配 |
| ``lan``       | 笔记本全量微调（同一局域网） | 分钟级 | 主路径 |

**推理不依赖本模块**：ultralytics 只在训练/评估时出现，检测器只需
opencv + numpy。因此树莓派跑作业时不会被训练依赖拖累。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .dataset import Dataset

logger = logging.getLogger("oceanus.grasp.train")

ProgressCb = Callable[[str, dict[str, Any]], None]


class TrainingError(RuntimeError):
    """训练不可用（缺 ultralytics）或训练过程失败。"""


@dataclass(frozen=True)
class Tier:
    key: str
    label: str
    epochs: int
    imgsz: int
    freeze: int
    batch: int
    note: str


TIERS: dict[str, Tier] = {
    "calibrate": Tier("calibrate", "免训练校准", 0, 320, 0, 8,
                      "不训练：扫阈值 + 写调参，秒级完成，演示日兜底"),
    "local": Tier("local", "Pi 本地微调", 30, 320, 10, 8,
                  "冻结骨干只训检测头，树莓派上可直接跑完"),
    "lan": Tier("lan", "局域网协同", 80, 416, 0, 16,
                "笔记本全量微调（同一局域网），权重回传树莓派"),
}


def training_available() -> bool:
    try:
        import ultralytics  # noqa: F401
    except ImportError:
        return False
    return True


def _base_weights(cfg: dict[str, Any]) -> str:
    """续训优先用当前 active 的 .pt；没有就退回官方预训练权重 yolov8n.pt。

    ultralytics 会自动下载 yolov8n.pt —— 现场无外网时请预先放到工作目录。
    """
    active = Path(__file__).resolve().parent.parent / "models" / "active" / "best.pt"
    if active.exists():
        return str(active)
    return "yolov8n.pt"


def imgsz_for(cfg: dict[str, Any], tier: "Tier") -> tuple[int, int]:
    """返回 ``(训练边长, 导出边长)``，两者**必须一致**且等于部署输入边长。

    ★ 为什么不能按档位各用各的尺寸：``Detector`` 按 ``vision.input_size`` 做
    letterbox，导出的 ONNX 若边长不同，``cv2.dnn`` 加载时直接报维度不匹配。
    原先 ``lan`` 档是按 ``TIERS`` 的 416 训练**并导出 416**，而 config 写的是
    640 —— 于是「作业台里训练完 → 部署 → 推理」必然崩，正是 ``config.yaml``
    「训练本来是 768，却导了 416」那条注释警告过的失败类别。
    档位里的 ``imgsz`` 现在只作为 config 没写 ``input_size`` 时的兜底。

    抽成纯函数是为了能在没有 ultralytics 的机器上单测这条口径。
    """
    size = int((cfg.get("vision", {}) or {}).get("input_size", tier.imgsz))
    return size, size


def train(
    cfg: dict[str, Any],
    tier_key: str,
    *,
    dataset_yaml: str | Path | None = None,
    progress_cb: ProgressCb | None = None,
) -> dict[str, Any]:
    """按档位训练并导出 ONNX，返回 {model_path, metrics, tier, output_dir}。"""
    tier = TIERS.get(str(tier_key))
    if tier is None:
        raise TrainingError(f"未知训练档位 {tier_key!r}；可选 {sorted(TIERS)}")

    # 免训练校准档不训练，交给 evaluate 的阈值扫描
    if tier.key == "calibrate":
        from .evaluate import sweep_threshold

        result = sweep_threshold(cfg, dataset_yaml=dataset_yaml)
        if progress_cb:
            progress_cb("done", {"stage": "calibrate", "result": result})
        return {"tier": tier.key, "model_path": None, **result}

    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise TrainingError(
            "训练需要 ultralytics：请在训练机上 `pip install ultralytics`；"
            "树莓派本地微调档亦需安装（推理不需要它）。"
        ) from exc

    dataset = Dataset.from_config(cfg)
    yaml_path = Path(dataset_yaml) if dataset_yaml else Path(dataset.build()["data_yaml"])
    if not yaml_path.exists():
        raise TrainingError(f"数据集描述文件不存在: {yaml_path}")

    train_imgsz, export_imgsz = imgsz_for(cfg, tier)

    model = YOLO(_base_weights(cfg))

    def _on_epoch_end(trainer: Any) -> None:  # pragma: no cover - 需 ultralytics
        if progress_cb is None:
            return
        metrics = getattr(trainer, "metrics", {}) or {}
        progress_cb("epoch", {
            "stage": "train",
            "epoch": int(getattr(trainer, "epoch", 0)) + 1,
            "total_epochs": tier.epochs,
            "loss": float(getattr(trainer, "loss", 0.0) or 0.0),
            "map50": float(metrics.get("metrics/mAP50(B)", 0.0) or 0.0),
            "map50_95": float(metrics.get("metrics/mAP50-95(B)", 0.0) or 0.0),
        })

    model.add_callback("on_fit_epoch_end", _on_epoch_end)

    results = model.train(
        data=str(yaml_path),
        epochs=tier.epochs,
        imgsz=train_imgsz,
        batch=tier.batch,
        freeze=tier.freeze,
        project=str(Path(dataset.root).parent / "runs"),
        name=f"grasp_{tier.key}",
        exist_ok=True,
        verbose=False,
    )

    save_dir = Path(getattr(results, "save_dir", "") or "")
    best_pt = save_dir / "weights" / "best.pt"
    if not best_pt.exists():
        raise TrainingError(f"训练结束但未找到 best.pt（{save_dir}）")

    if progress_cb:
        progress_cb("export", {"stage": "export",
                               "message": f"导出 ONNX imgsz={export_imgsz}"
                                          f"（训练 {train_imgsz}）"})

    onnx_path = _export_onnx(model, best_pt, export_imgsz)

    metrics = {
        "map50": float(getattr(results, "box", None).map50) if getattr(results, "box", None) else None,
        "map50_95": float(getattr(results, "box", None).map) if getattr(results, "box", None) else None,
    }
    out = {
        "tier": tier.key,
        "model_path": str(onnx_path or best_pt),
        "onnx": str(onnx_path) if onnx_path else None,
        "best_pt": str(best_pt),
        "output_dir": str(save_dir),
        "train_imgsz": train_imgsz,
        "export_imgsz": export_imgsz,
        "metrics": metrics,
    }
    if progress_cb:
        progress_cb("done", {"stage": "done", **out})
    logger.info("训练完成 tier=%s model=%s", tier.key, out["model_path"])
    return out


def _export_onnx(model: Any, best_pt: Path, imgsz: int) -> Path | None:
    """导出 ONNX（推理首选格式：零额外依赖、Pi 上比 .pt 快）。"""
    try:
        exported = model.export(format="onnx", imgsz=imgsz, half=False, dynamic=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ONNX 导出失败（将退回 .pt）: %s", exc)
        return None
    path = Path(str(exported))
    if path.exists():
        return path
    candidate = best_pt.with_suffix(".onnx")
    return candidate if candidate.exists() else None


# ----------------------------------------------------------------------
# 命令行入口：Pi 上不依赖作业台也能训练
#   python3 edge/grasp/vision/train.py --tier local
#   python3 edge/grasp/vision/train.py --eval-only
# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    here = Path(__file__).resolve().parent
    grasp_dir = here.parent
    edge_dir = grasp_dir.parent
    for p in (str(edge_dir), str(grasp_dir)):
        if p not in sys.path:
            sys.path.insert(0, p)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    ap = argparse.ArgumentParser(description="Oceanus 抓取模型训练/评估")
    ap.add_argument("--config", default=None)
    ap.add_argument("--tier", default="local", choices=sorted(TIERS))
    ap.add_argument("--dataset-yaml", default=None)
    ap.add_argument("--eval-only", action="store_true", help="只评估 active 模型，不训练")
    args = ap.parse_args(argv)

    # ★ 用绝对包路径导入：脚本方式运行（python edge/grasp/vision/train.py）时
    #   __package__ 为空，相对导入会直接报错；绝对导入两种运行方式都成立。
    from grasp.config import load_config

    cfg = load_config(args.config)

    if args.eval_only:
        from grasp.vision.evaluate import evaluate

        metrics = evaluate(cfg, dataset_yaml=args.dataset_yaml)
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        return 0

    def _cb(stage: str, info: dict[str, Any]) -> None:
        if stage == "epoch":
            print(f"  epoch {info['epoch']}/{info['total_epochs']} "
                  f"loss={info.get('loss', 0):.4f} mAP50={info.get('map50', 0):.4f}")
        elif stage in ("export", "start"):
            print(f"  {stage} …")

    result = train(cfg, args.tier, dataset_yaml=args.dataset_yaml, progress_cb=_cb)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
