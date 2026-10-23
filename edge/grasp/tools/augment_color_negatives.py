#!/usr/bin/env python3
"""彩色干扰负样本增强：让模型学会「彩色 ≠ 塑料袋」。

## 为什么必须做这一步（实测驱动，不是拍脑袋）

``tools/eval_robustness.py`` 的实测结论：在测试图里注入高饱和彩色圆块后，
当前模型的检出数**从 695 暴涨到 1412（+103%）** —— 它会被彩色物体严重带偏。

根因很明确：公开基准 SC-FloatWaste 是**河流漂浮垃圾**数据集，画面里的
干扰物几乎都是低饱和的（泡沫、水草），**几乎没有鲜艳彩色物体**。模型因此
从未学过"彩色物体不是塑料袋"，见到高饱和色块就倾向于报袋。

## 做法

在**训练/验证**图里注入高饱和彩色物体（多种形状与颜色），**且不给它们标注** ——
于是它们成为「未标注背景负样本」，模型必须学会忽略。这是标准的负样本挖掘思路。

**关键约束：注入位置必须避开真值塑料袋框。** 否则彩色物体压住真塑料袋，
画面与标注就对不上，等于制造脏数据 —— 那样训出来的模型只会更差。
实现上采用「先采样定框、再一次性绘制」，全程不改动已绘内容，不产生撤销操作。

test 划分**不做注入**：抗干扰效果的独立验证由 ``eval_robustness.py`` 在评测时
实时注入完成 —— 训练见过的注入批次与评测时的独立注入批次不同，避免自证。

用法::

    python edge/grasp/tools/augment_color_negatives.py --per-image 3
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GRASP_DIR = HERE.parent
for _p in (str(GRASP_DIR), str(GRASP_DIR.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

DEFAULT_SRC = GRASP_DIR / "data" / "public_plastic"
DEFAULT_OUT = GRASP_DIR / "data" / "public_plastic_color"
#: 注入只作用于这两个划分；test 保持干净，交给评测端独立注入
AUGMENT_SPLITS = ("train", "val")
COLORS = [(0, 0, 235), (235, 80, 0), (0, 205, 255), (0, 180, 0),
          (200, 0, 200), (30, 30, 240), (0, 140, 255)]

#: 与真值塑料袋框的 IoU 超过它就判定"压住了真目标"，撤掉重采
GT_OVERLAP_MAX = 0.02


def _iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1, ix2, iy2 = max(ax1, bx1), max(ay1, by1), min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _read_boxes(lbl: Path, w: int, h: int) -> list[tuple[float, float, float, float]]:
    if not lbl.exists():
        return []
    out = []
    for line in lbl.read_text(encoding="utf-8").splitlines():
        p = line.split()
        if len(p) != 5:
            continue
        cx, cy, bw, bh = (float(x) for x in p[1:])
        out.append(((cx - bw / 2) * w, (cy - bh / 2) * h,
                    (cx + bw / 2) * w, (cy + bh / 2) * h))
    return out


def sample_blob(rng: np.random.Generator, w: int, h: int) -> dict:
    """采样一个彩色物体的参数（**不绘制**）。

    形状三类：``circle`` 圆球形浮标 / ``rect`` 矩形色块 / ``strip`` 细长漂浮物。
    返回 dict 含 ``kind``、``color``、``bbox`` 与几何参数，供绘制与避让检查共用。
    """
    kind = ("circle", "rect", "strip")[int(rng.integers(0, 3))]
    color = COLORS[int(rng.integers(0, len(COLORS)))]
    if kind == "circle":
        r = int(rng.integers(12, 36))
        cx = int(rng.integers(r + 2, max(r + 3, w - r - 2)))
        cy = int(rng.integers(r + 2, max(r + 3, h - r - 2)))
        return {"kind": kind, "color": color, "cx": cx, "cy": cy, "r": r,
                "bbox": (cx - r, cy - r, cx + r, cy + r)}
    if kind == "rect":
        bw, bh = int(rng.integers(18, 60)), int(rng.integers(14, 46))
        x = int(rng.integers(2, max(3, w - bw - 2)))
        y = int(rng.integers(2, max(3, h - bh - 2)))
        return {"kind": kind, "color": color, "x": x, "y": y, "w": bw, "h": bh,
                "bbox": (x, y, x + bw, y + bh)}
    lw, lh = int(rng.integers(30, 120)), int(rng.integers(5, 13))
    x = int(rng.integers(2, max(3, w - lw - 2)))
    y = int(rng.integers(2, max(3, h - lh - 2)))
    return {"kind": kind, "color": color, "x": x, "y": y, "w": lw, "h": lh,
            "bbox": (x, y, x + lw, y + lh)}


def render_blob(img, blob: dict) -> None:
    """按采样结果绘制（就地修改 img）。"""
    color = blob["color"]
    if blob["kind"] == "circle":
        cx, cy, r = blob["cx"], blob["cy"], blob["r"]
        cv2.circle(img, (cx, cy), r, color, -1)
        cv2.circle(img, (cx, cy), r, (250, 250, 250), 2)
        cv2.circle(img, (cx - r // 3, cy - r // 3), max(2, r // 4), (255, 255, 255), -1)
    else:
        x, y, w, h = blob["x"], blob["y"], blob["w"], blob["h"]
        cv2.rectangle(img, (x, y), (x + w, y + h), color, -1)
        if blob["kind"] == "rect":
            cv2.rectangle(img, (x, y), (x + w, y + h), (250, 250, 250), 2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="注入彩色干扰负样本（不标注）")
    ap.add_argument("--src", default=str(DEFAULT_SRC))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--per-image", type=int, default=3,
                    help="每图注入个数的上界 N（实际随机 0..2N）")
    ap.add_argument("--max-tries", type=int, default=40, help="为避开真值框的重采样次数")
    ap.add_argument("--seed", type=int, default=20261015)
    args = ap.parse_args(argv)

    src, out = Path(args.src), Path(args.out)
    rng = np.random.default_rng(args.seed)
    stats: dict[str, dict] = {}

    for split in ("train", "val", "test"):
        img_src, lbl_src = src / "images" / split, src / "labels" / split
        img_dst, lbl_dst = out / "images" / split, out / "labels" / split
        img_dst.mkdir(parents=True, exist_ok=True)
        lbl_dst.mkdir(parents=True, exist_ok=True)

        images = sorted(img_src.glob("*.jpg")) + sorted(img_src.glob("*.png"))
        injected = skipped = 0

        for p in images:
            img = cv2.imread(str(p))
            if img is None:
                continue
            h, w = img.shape[:2]
            lbl = lbl_src / f"{p.stem}.txt"

            if split in AUGMENT_SPLITS:
                gt = _read_boxes(lbl, w, h)
                placed: list[dict] = []
                want = int(rng.integers(0, max(1, args.per_image * 2 + 1)))
                for _ in range(want):
                    for _try in range(args.max_tries):
                        cand = sample_blob(rng, w, h)
                        # 只避开真值框；已放置的彼此重叠无妨（真实水面也常有堆叠）
                        if all(_iou(cand["bbox"], g) <= GT_OVERLAP_MAX for g in gt):
                            placed.append(cand)
                            break
                    else:
                        skipped += 1
                for cand in placed:              # 采样全部完成后一次性绘制
                    render_blob(img, cand)
                injected += len(placed)

            cv2.imwrite(str(img_dst / p.name), img,
                        [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            (lbl_dst / f"{p.stem}.txt").write_text(
                lbl.read_text(encoding="utf-8") if lbl.exists() else "", encoding="utf-8")

        stats[split] = {"images": len(images), "injected": injected,
                        "skipped_placements": skipped}

    (out / "data.yaml").write_text(
        "# 由 edge/grasp/tools/augment_color_negatives.py 生成\n"
        "# train/val 注入高饱和彩色物体（未标注）作背景负样本；test 保持干净。\n"
        f"path: {out.as_posix()}\n"
        "train: images/train\nval: images/val\ntest: images/test\n"
        "names:\n  0: plastic_bag\n",
        encoding="utf-8",
    )
    (out / "PROVENANCE.json").write_text(json.dumps({
        "built_by": "edge/grasp/tools/augment_color_negatives.py",
        "base_dataset": str(src),
        "augmented_splits": list(AUGMENT_SPLITS),
        "clean_splits": ["test"],
        "per_image_upper_bound": args.per_image,
        "shapes": ["circle_buoy", "rect_block", "long_strip"],
        "colors_bgr": COLORS,
        "constraint": f"注入位置避开真值塑料袋框（IoU<={GT_OVERLAP_MAX}），避免制造脏标注",
        "claim_boundary": ("彩色物体为合成注入，用于降低对高饱和颜色的误敏；"
                           "不代表现场彩色浮标的真实测试结论"),
        "stats": stats,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"[完成] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
