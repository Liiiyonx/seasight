#!/usr/bin/env python3
"""构建单类「塑料袋」数据集：从 SC-FloatWaste 四契约类里抽出 plastic。

## 为什么单独写一个构建器，而不用 vision/dataset.py 的 build()

``vision/dataset.py`` 的 ``build()`` 会**随机切分** train/val —— 那对自采数据没问题，
但用在公开基准上会**破坏数据集发布方的官方隔离**（还可能把同一段视频的相邻帧分到
两边）。项目既有纪律明确要求：公开基准的 train/val/test 必须沿用官方划分，
不做任何随机切分（见 ``docs/competitions/public-floatwaste-benchmark.md`` §2.2）。
因此这里严格按官方 split 目录直搬，**不含随机性**。

## 类别处理

四契约类索引：``0=foam 1=plastic 2=fishing_gear 3=other``（
``ml/scripts/evaluate_public_litter.py`` 的 ``CONTRACT4_NAMES``）。
本构建器**只保留 plastic（索引 1）并重映射为 0**，输出单类 ``plastic_bag``。

★ 关键设计：**保留全部图像，只丢弃非 plastic 的框**。
这样 foam / fishing_gear / other 类目标就以「未标注背景」的身份留在画面里 ——
模型必须学会「这些漂浮物不是塑料袋」，这正是"彩色浮标不干扰"所需要的负样本来源。
若把它们整图剔除，模型反而会倾向于把任何漂浮物都报成袋。

> ⚠️ 口径边界：本数据集来自第三方公开基准（CC BY 4.0），**不是连江现场数据**。
> 用它训出的精度只能表述为「公开数据集上的精度」，不得写成「现场验证精度」。

用法::

    python edge/grasp/tools/build_plastic_dataset.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
GRASP_DIR = HERE.parent
REPO_ROOT = GRASP_DIR.parent.parent
DEFAULT_SRC = REPO_ROOT / "ml" / "datasets" / "public_floatwaste"
DEFAULT_OUT = GRASP_DIR / "data" / "public_plastic"

# 四契约类索引（与 ml/scripts/evaluate_public_litter.py 的 CONTRACT4_NAMES 一致）
CONTRACT4_NAMES = ("foam", "plastic", "fishing_gear", "other")
PLASTIC_INDEX = CONTRACT4_NAMES.index("plastic")
TARGET_CLASS_NAME = "plastic_bag"
SPLITS = ("train", "val", "test")


def _link_or_copy(src: Path, dst: Path) -> str:
    """优先硬链接（同卷零拷贝），失败则复制。"""
    if dst.exists():
        return "exists"
    try:
        os.link(src, dst)
        return "link"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


def build(src_dir: Path, out_dir: Path, keep_background: bool) -> dict:
    if not (src_dir / "labels_contract4").exists():
        raise SystemExit(
            f"找不到 {src_dir / 'labels_contract4'}。请先按 "
            "docs/competitions/public-floatwaste-benchmark.md §2.3 下载公开数据。"
        )

    stats: dict[str, dict] = {}
    total = Counter()

    for split in SPLITS:
        img_src = src_dir / "images" / split
        lbl_src = src_dir / "labels_contract4" / split
        img_dst = out_dir / "images" / split
        lbl_dst = out_dir / "labels" / split
        img_dst.mkdir(parents=True, exist_ok=True)
        lbl_dst.mkdir(parents=True, exist_ok=True)

        images = sorted(img_src.glob("*.jpg")) + sorted(img_src.glob("*.png"))
        kept_imgs = 0
        kept_boxes = 0
        dropped_boxes = 0
        bg_images = 0
        modes: Counter = Counter()

        for img in images:
            lbl = lbl_src / f"{img.stem}.txt"
            rows: list[str] = []
            if lbl.exists():
                for line in lbl.read_text(encoding="utf-8").splitlines():
                    parts = line.split()
                    if len(parts) != 5:
                        continue
                    if int(parts[0]) == PLASTIC_INDEX:
                        # 重映射为单类索引 0，只改类号，坐标原样保留
                        rows.append("0 " + " ".join(parts[1:]))
                        kept_boxes += 1
                    else:
                        dropped_boxes += 1

            if not rows and not keep_background:
                continue          # 无塑料框且不留背景 → 丢弃该图

            if not rows:
                bg_images += 1    # 留下作纯背景负样本

            modes[_link_or_copy(img, img_dst / img.name)] += 1
            (lbl_dst / f"{img.stem}.txt").write_text(
                "\n".join(rows) + ("\n" if rows else ""), encoding="utf-8"
            )
            kept_imgs += 1

        stats[split] = {
            "images": kept_imgs,
            "plastic_boxes": kept_boxes,
            "background_only_images": bg_images,
            "non_plastic_boxes_as_background": dropped_boxes,
            "file_mode": dict(modes),
        }
        total["images"] += kept_imgs
        total["plastic_boxes"] += kept_boxes
        total["background_only_images"] += bg_images

    data_yaml = out_dir / "data.yaml"
    data_yaml.write_text(
        "# 由 edge/grasp/tools/build_plastic_dataset.py 生成，请勿手工编辑\n"
        "# 来源：SC-FloatWaste (FloatWaste)，CC BY 4.0，DOI 10.5281/zenodo.21934286\n"
        "# 类别：单类 plastic_bag（由四契约类 plastic 重映射而来）\n"
        f"path: {out_dir.as_posix()}\n"
        "train: images/train\n"
        "val: images/val\n"
        "test: images/test\n"
        "names:\n  0: plastic_bag\n",
        encoding="utf-8",
    )

    prov = {
        "schema_version": "1.0",
        "built_by": "edge/grasp/tools/build_plastic_dataset.py",
        "source_dataset": "SC-FloatWaste (FloatWaste)",
        "source_dir": str(src_dir),
        "hf_id": "OliviaWuYJ/sc-floatwaste",
        "zenodo_doi": "10.5281/zenodo.21934286",
        "license": "CC BY 4.0",
        "citation": ("Wu, Yujiao et al. FloatWaste: a multi-region riverine "
                     "floating-waste detection benchmark (CC BY 4.0)"),
        "label_space": {"source": list(CONTRACT4_NAMES), "kept_index": PLASTIC_INDEX,
                        "output_classes": [TARGET_CLASS_NAME]},
        "split_policy": "沿用数据集发布方官方划分，未做任何随机切分",
        "background_policy": (
            "保留全部图像；非 plastic 的框一律去除，使 foam/fishing_gear/other "
            "成为未标注背景负样本"
        ),
        "stats": stats,
        "totals": dict(total),
        "claim_boundary": (
            "第三方公开基准数据，非连江现场、非本项目设备。基于它训练的精度只能表述为"
            "「公开数据集上的精度」，禁止写成「现场验证精度」。"
        ),
    }
    (out_dir / "PROVENANCE.json").write_text(
        json.dumps(prov, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return prov


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="构建单类塑料袋数据集（公开基准）")
    ap.add_argument("--src", default=str(DEFAULT_SRC))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--drop-background", action="store_true",
                    help="丢弃无塑料框的图（默认保留作背景负样本）")
    args = ap.parse_args(argv)

    prov = build(Path(args.src), Path(args.out), keep_background=not args.drop_background)
    print(json.dumps({"stats": prov["stats"], "totals": prov["totals"]},
                     ensure_ascii=False, indent=2))
    print(f"[完成] 输出目录 {args.out}")
    print(f"[清单] data.yaml = {Path(args.out) / 'data.yaml'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
