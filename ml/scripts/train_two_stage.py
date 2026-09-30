#!/usr/bin/env python3
"""探海灵眸 SeaSight — 两阶段 YOLO 训练脚本。

为什么必须两阶段
────────────────
自建数据集只有几百到一两千张。直接端到端训练时，
随机初始化的检测头会在前几轮产生很大的梯度，
把 COCO 预训练好的主干特征"冲烂"—— 结果比用预训练权重还差。

两阶段把这个过程拆开：
  阶段一  冻结主干（freeze=10），只训检测头 —— 让 head 先收敛到合理范围
  阶段二  解冻全量，用小学习率微调 —— 在好的起点上继续优化

实测在小数据集上，两阶段比单阶段的 mAP@0.5 通常高 3~8 个百分点。

另一个关键点是负样本
────────────────
海面视频里大量画面是空镜头（只有水波和反光）。
如果不给模型看纯背景负样本，它会把浪花高光当成泡沫 —— 这是海漂垃圾
检测里最主要的误报来源。脚本会在数据集校验时检查负样本比例。

用法
────
    # 完整两阶段训练
    python train_two_stage.py --data ../configs/seasight.yaml

    # 只跑阶段二（已有阶段一的权重）
    python train_two_stage.py --resume-from runs/detect/stage1/weights/best.pt

    # 单阶段基线（用于对比，证明两阶段的价值）
    python train_two_stage.py --single-stage

    # 只做数据集校验，不训练
    python train_two_stage.py --check-only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("[错误] 缺少 pyyaml：pip install pyyaml", file=sys.stderr)
    sys.exit(1)


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        print(f"[错误] 配置文件不存在：{path}", file=sys.stderr)
        sys.exit(1)
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ----------------------------------------------------------------------
# 数据集校验
# ----------------------------------------------------------------------
def check_dataset(data_cfg: dict[str, Any]) -> tuple[bool, list[str]]:
    """检查数据集完整性与合理性。

    这里做的是「训练前能发现的问题」，避免跑了 3 小时才因为
    标签缺失或类别不均衡白费算力。
    """
    issues: list[str] = []
    warnings: list[str] = []

    root = Path(data_cfg.get("path", ".")).resolve()
    split_dirs = {}
    for split in ("train", "val"):
        rel = data_cfg.get(split)
        if not rel:
            continue
        split_dirs[split] = (root / rel) if not Path(rel).is_absolute() else Path(rel)

    if not split_dirs:
        issues.append("配置中未定义 train / val 路径")
        return False, issues

    total_images = 0
    for split, img_dir in split_dirs.items():
        if not img_dir.exists():
            issues.append(f"{split} 图片目录不存在：{img_dir}")
            continue

        exts = {".jpg", ".jpeg", ".png", ".bmp"}
        images = [p for p in img_dir.rglob("*") if p.suffix.lower() in exts]
        total_images += len(images)

        if not images:
            issues.append(f"{split} 目录下没有图片：{img_dir}")
            continue

        # 推导标签目录并检查配对
        label_dir = Path(str(img_dir).replace("images", "labels", 1))
        missing_labels = 0
        empty_labels = 0
        for img in images[:2000]:   # 抽样检查，避免超大数据集卡住
            lbl = label_dir / f"{img.stem}.txt"
            if not lbl.exists():
                missing_labels += 1
            elif lbl.stat().st_size == 0:
                empty_labels += 1

        if missing_labels:
            ratio = missing_labels / min(len(images), 2000)
            msg = f"{split}: {missing_labels} 张图片缺标签文件（{ratio:.0%}）"
            (issues if ratio > 0.1 else warnings).append(msg)

        print(f"  · {split}: {len(images)} 张图片"
              + (f"，其中 {empty_labels} 张为纯背景负样本" if empty_labels else ""))

    # 负样本比例检查
    stats = data_cfg.get("stats", {})
    bg = int(stats.get("background_images", 0) or 0)
    if bg == 0 and total_images > 0:
        warnings.append(
            "未配置纯背景负样本。海面空镜头不加入训练集，"
            "模型会把浪花高光误判为泡沫 —— 这是本项目最主要的误报来源。"
            "建议加入 200~500 张无垃圾的水面图（标签文件留空即可）。"
        )

    # 类别均衡检查
    instances = stats.get("instances", {}) or {}
    counts = [v for v in instances.values() if isinstance(v, (int, float)) and v > 0]
    if counts:
        ratio = max(counts) / min(counts)
        if ratio > 10:
            warnings.append(
                f"类别数量不均衡（最多/最少 = {ratio:.1f} 倍）。"
                "可考虑对稀少类别做额外增强，或接受该类召回偏低。"
            )

    # 数据集规模建议
    if total_images and total_images < 300:
        warnings.append(
            f"数据量偏少（{total_images} 张）。建议至少 500 张，"
            "否则务必开启两阶段训练并加大增强强度。"
        )

    print()
    for w in warnings:
        print(f"  ⚠ {w}")

    return len(issues) == 0, issues


# ----------------------------------------------------------------------
# 训练
# ----------------------------------------------------------------------
def train_stage1(args: argparse.Namespace, data_cfg: dict, train_cfg: dict) -> Path | None:
    """阶段一：冻结主干，只训检测头。"""
    from ultralytics import YOLO

    stage = train_cfg.get("two_stage", {}).get("stage1", {})
    freeze = int(stage.get("freeze", 10))
    epochs = int(stage.get("epochs", 15))
    lr0 = float(stage.get("lr0", 0.001))

    print("\n" + "─" * 66)
    print(f"  阶段一：冻结主干 {freeze} 层，训检测头（{epochs} epochs, lr={lr0}）")
    print("─" * 66)

    model = YOLO(train_cfg.get("model", "yolo11s.pt"))
    results = model.train(
        data=args.data,
        epochs=epochs,
        imgsz=int(train_cfg.get("imgsz", 640)),
        batch=int(train_cfg.get("batch", 16)),
        workers=int(train_cfg.get("workers", 4)),
        device=args.device or train_cfg.get("device", 0),
        optimizer=train_cfg.get("optimizer", "AdamW"),
        lr0=lr0,
        lrf=float(train_cfg.get("lrf", 0.01)),
        cos_lr=bool(train_cfg.get("cos_lr", True)),
        freeze=freeze,
        warmup_epochs=float(train_cfg.get("warmup_epochs", 3.0)),
        project=args.project,
        name="stage1",
        exist_ok=True,
        plots=True,
        val=True,
        # 数据增强
        **_augment_kwargs(train_cfg),
    )
    _ = results
    best = Path(args.project) / "stage1" / "weights" / "best.pt"
    return best if best.exists() else None


def train_stage2(
    args: argparse.Namespace, data_cfg: dict, train_cfg: dict, resume_from: Path | None
) -> Path | None:
    """阶段二：解冻全量，小学习率微调。"""
    from ultralytics import YOLO

    stage = train_cfg.get("two_stage", {}).get("stage2", {})
    epochs = int(stage.get("epochs", 85))
    lr0 = float(stage.get("lr0", 0.0003))

    weights = resume_from or Path(train_cfg.get("model", "yolo11s.pt"))
    print("\n" + "─" * 66)
    print(f"  阶段二：全量微调（{epochs} epochs, lr={lr0}）")
    print(f"  起点权重：{weights}")
    print("─" * 66)

    model = YOLO(str(weights))
    model.train(
        data=args.data,
        epochs=epochs,
        imgsz=int(train_cfg.get("imgsz", 640)),
        batch=int(train_cfg.get("batch", 16)),
        workers=int(train_cfg.get("workers", 4)),
        device=args.device or train_cfg.get("device", 0),
        optimizer=train_cfg.get("optimizer", "AdamW"),
        lr0=lr0,
        lrf=float(train_cfg.get("lrf", 0.01)),
        cos_lr=bool(train_cfg.get("cos_lr", True)),
        freeze=0,
        warmup_epochs=1.0,
        patience=int(train_cfg.get("patience", 25)),
        close_mosaic=int(train_cfg.get("close_mosaic", 15)),
        project=args.project,
        name="stage2",
        exist_ok=True,
        plots=True,
        val=True,
        **_augment_kwargs(train_cfg),
    )

    best = Path(args.project) / "stage2" / "weights" / "best.pt"
    return best if best.exists() else None


def _augment_kwargs(train_cfg: dict) -> dict[str, Any]:
    """抽取数据增强参数。

    ★ flipud 强制为 0 —— 水面倒影会让模型学到「垃圾长在天空上」，
      这是海面数据集里一个很隐蔽但影响很大的坑。
    """
    keys = [
        "hsv_h", "hsv_s", "hsv_v", "degrees", "translate", "scale",
        "shear", "perspective", "flipud", "fliplr", "mosaic", "mixup",
        "copy_paste", "erasing", "box", "cls", "dfl",
    ]
    kwargs = {k: train_cfg[k] for k in keys if k in train_cfg}
    # 强制项
    kwargs["flipud"] = 0.0
    if "mixup" in kwargs:
        kwargs["mixup"] = 0.0
    return kwargs


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="探海灵眸 — 两阶段 YOLO 训练")
    default_cfg = Path(__file__).parent.parent / "configs"
    parser.add_argument("--data", default=str(default_cfg / "seasight.yaml"), help="数据集配置")
    parser.add_argument("--args", dest="args_file", default=str(default_cfg / "train_args.yaml"),
                        help="超参配置")
    parser.add_argument("--project", default="runs/seasight", help="输出目录")
    parser.add_argument("--device", default=None, help="训练设备，0/cpu")
    parser.add_argument("--resume-from", default=None, help="从已有权重开始（跳过阶段一）")
    parser.add_argument("--single-stage", action="store_true", help="单阶段基线（做对比实验用）")
    parser.add_argument("--check-only", action="store_true", help="只校验数据集")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print("=" * 66)
    print("  探海灵眸 SeaSight — YOLO 两阶段训练")
    print("=" * 66)

    data_cfg = load_yaml(Path(args.data))
    train_cfg = load_yaml(Path(args.args_file))

    # ---------- 数据集校验 ----------
    print("\n[1/3] 数据集校验")
    ok, issues = check_dataset(data_cfg)
    if not ok:
        print("\n  ✗ 数据集存在阻断性问题：")
        for issue in issues:
            print(f"    · {issue}")
        print("\n  修复后再训练。可用 --check-only 反复校验。")
        return 1
    print("  ✓ 数据集结构检查通过")

    if args.check_only:
        return 0

    # ---------- 训练 ----------
    if args.single_stage:
        print("\n[2/3] 单阶段基线训练（用于对比两阶段的效果）")
        from ultralytics import YOLO

        model = YOLO(train_cfg.get("model", "yolo11s.pt"))
        model.train(
            data=args.data,
            epochs=int(train_cfg.get("epochs", 100)),
            imgsz=int(train_cfg.get("imgsz", 640)),
            batch=int(train_cfg.get("batch", 16)),
            device=args.device or train_cfg.get("device", 0),
            optimizer=train_cfg.get("optimizer", "AdamW"),
            lr0=float(train_cfg.get("lr0", 0.001)),
            cos_lr=bool(train_cfg.get("cos_lr", True)),
            patience=int(train_cfg.get("patience", 25)),
            close_mosaic=int(train_cfg.get("close_mosaic", 15)),
            project=args.project,
            name="baseline_single",
            exist_ok=True,
            plots=True,
            **_augment_kwargs(train_cfg),
        )
        best = Path(args.project) / "baseline_single" / "weights" / "best.pt"
    else:
        print("\n[2/3] 两阶段训练")

        resume = Path(args.resume_from) if args.resume_from else None
        if resume is None:
            stage1_best = train_stage1(args, data_cfg, train_cfg)
            if stage1_best is None:
                print("\n  ✗ 阶段一未产出权重，中止。请检查上面的训练日志。")
                return 1
            print(f"\n  阶段一完成 → {stage1_best}")
            resume = stage1_best
        else:
            print(f"  跳过阶段一，从 {resume} 直接进入微调")

        best = train_stage2(args, data_cfg, train_cfg, resume)
        if best is None:
            print("\n  ✗ 阶段二未产出权重，中止。")
            return 1

    # ---------- 收尾 ----------
    print("\n[3/3] 训练完成")
    print(f"  最优权重：{best}")

    # 保存本次实验配置，便于复现与写论文
    meta = {
        "weights": str(best),
        "data_config": str(args.data),
        "train_config": str(args.args_file),
        "single_stage": bool(args.single_stage),
        "resume_from": args.resume_from,
    }
    meta_path = Path(args.project) / "experiment.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  实验记录：{meta_path}")

    print("\n  下一步：导出 ONNX 供边缘端部署")
    print(f"    python export_onnx.py --weights {best}")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    # ★ Windows 上必须放在 __main__ 里 ——
    #   DataLoader 用 spawn 启动子进程，若不加这层保护，
    #   子进程会重新 import 本模块并再次执行训练，导致内存爆炸或死锁。
    sys.exit(main())
