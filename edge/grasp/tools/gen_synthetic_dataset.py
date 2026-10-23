#!/usr/bin/env python3
"""合成数据集生成工具：产出「帧 + 精确真值标注」的 YOLO 数据集。

## 用途与边界（务必先读）

真机/真实样本未到位时，用它把**数据→训练→评估→部署**这条管线真实跑一遍，
产出一个能真正被 ``Detector`` 加载的模型。这样做的价值是：

* 管线被真实执行过（数据集组织、训练、留出集评估、门禁、原子部署、回滚）；
* 无真机时作业台也能演示「模型加载 + 真推理」，而不是只靠 HSV 合成检测。

**它的边界必须说清**：合成数据训出的权重只在合成画面分布内有效 ——
它**不代表现场精度**，也不能替代真实采集。对外只能表述为
「管线验证 / 演示模型」，不得写成「已训练出可用的现场模型」。
真机到位后必须用作业台采集真实样本重训一次。

## 视觉配方

沿用 ``arm_io._mock_frame`` 的颜色先验（塑料袋低饱和浅色、浮标高饱和彩色），
但把**位置、数量、尺寸、角度、光照、水面反光**全部随机化，避免模型退化成
「靠颜色阈值找团块」：

* 塑料袋 1~2 个：随机位置/尺寸/旋转，带褶皱多边形扰动
* 彩色浮标 2~5 个：随机位置/半径/颜色（负类样本来源）
* 背景：深色水面 + 水平渐变 + 随机高光带 + 高斯噪声 + 整体亮度扰动

物体一律完整落在画面内，因此标注框与可见目标严格对应（不引入脏标注）。

用法::

    python edge/grasp/tools/gen_synthetic_dataset.py --count 160 --tag synthetic_demo
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GRASP_DIR = HERE.parent
EDGE_DIR = GRASP_DIR.parent
for path in (str(EDGE_DIR), str(GRASP_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from grasp.config import load_config  # noqa: E402
from grasp.vision.dataset import Box, Dataset  # noqa: E402

BAG_CLASS = "plastic_bag"
BUOY_CLASS = "buoy"

#: 浮标颜色池（BGR，高饱和）
BUOY_COLORS = [
    (0, 0, 235), (235, 80, 0), (0, 205, 255), (0, 180, 0), (200, 0, 200), (30, 30, 240),
]

FRAME_W, FRAME_H = 640, 480
MARGIN = 12          # 物体必须离画面边缘至少这么多像素


def _background(rng: np.random.Generator) -> np.ndarray:
    """深色水面：水平渐变 + 随机高光带（模拟反光）+ 噪声 + 亮度扰动。"""
    img = np.zeros((FRAME_H, FRAME_W, 3), dtype=np.float32)
    base = rng.uniform(28, 48)
    # 远处（画面上方）略亮，近处略暗
    grad = np.linspace(base + rng.uniform(6, 18), base, FRAME_H).reshape(-1, 1, 1)
    img += grad
    img[:, :, 0] += rng.uniform(4, 14)      # 水面偏蓝
    img[:, :, 1] += rng.uniform(6, 16)

    # 高光带画在物体之前：它是"背景反光"，不遮挡目标，因此不污染标注
    for _ in range(rng.integers(0, 3)):
        y = int(rng.integers(20, FRAME_H - 20))
        h = int(rng.integers(3, 12))
        alpha = rng.uniform(0.10, 0.34)
        img[y:y + h, :, :] += 255.0 * alpha

    img += rng.normal(0, 3.5, img.shape)     # 传感器噪声
    img *= rng.uniform(0.86, 1.16)           # 整体光照
    return np.clip(img, 0, 255).astype(np.uint8)


def _draw_bag(rng: np.random.Generator, img: np.ndarray) -> tuple[int, int, int, int]:
    """画一个低饱和浅色"塑料袋"，返回像素 bbox (x1,y1,x2,y2)。"""
    w = int(rng.integers(56, 118))
    h = int(rng.integers(26, 62))
    cx = int(rng.integers(MARGIN + w // 2, FRAME_W - MARGIN - w // 2))
    cy = int(rng.integers(MARGIN + h // 2, FRAME_H - MARGIN - h // 2))
    angle = float(rng.uniform(-28, 28))

    # 褶皱扰动的多边形（比纯椭圆更像袋子）
    n = int(rng.integers(9, 13))
    pts = []
    for i in range(n):
        t = 2 * np.pi * i / n
        rr = 1.0 + rng.uniform(-0.13, 0.13)
        x = np.cos(t) * (w / 2) * rr
        y = np.sin(t) * (h / 2) * rr
        ca, sa = np.cos(np.radians(angle)), np.sin(np.radians(angle))
        pts.append([[cx + x * ca - y * sa, cy + x * sa + y * ca]])
    poly = np.array(pts, dtype=np.int32)

    # 袋面：低饱和浅色（白/灰），沿法线方向有明暗变化
    v = int(rng.integers(196, 238))
    fill = (v, min(255, v + 4), min(255, v + 8))
    cv2.fillPoly(img, [poly], fill)
    cv2.polylines(img, [poly], True, (int(v * 0.80),) * 3, 2)   # 略暗描边

    x1, y1 = max(0, cx - w // 2), max(0, cy - h // 2)
    x2, y2 = min(FRAME_W - 1, cx + w // 2), min(FRAME_H - 1, cy + h // 2)
    return x1, y1, x2, y2


def _draw_buoy(rng: np.random.Generator, img: np.ndarray) -> tuple[int, int, int, int]:
    """画一个高饱和彩色浮标（圆形 + 白边环），返回像素 bbox。"""
    r = int(rng.integers(15, 33))
    cx = int(rng.integers(MARGIN + r, FRAME_W - MARGIN - r))
    cy = int(rng.integers(MARGIN + r, FRAME_H - MARGIN - r))
    color = BUOY_COLORS[int(rng.integers(0, len(BUOY_COLORS)))]
    cv2.circle(img, (cx, cy), r, color, -1)
    cv2.circle(img, (cx, cy), r, (250, 250, 250), 2)
    # 高光点，避免模型只学到"纯色圆"
    hr = max(2, r // 4)
    cv2.circle(img, (cx - r // 3, cy - r // 3), hr, (255, 255, 255), -1)
    return cx - r, cy - r, cx + r, cy + r


def synth_frame(rng: np.random.Generator) -> tuple[np.ndarray, list[tuple[str, tuple[int, int, int, int]]]]:
    """生成一帧与它的真值框列表 [(class, (x1,y1,x2,y2))]。"""
    img = _background(rng)
    boxes: list[tuple[str, tuple[int, int, int, int]]] = []

    # 先画浮标（负类，必须进画面 —— 抗干扰样本就靠它）
    for _ in range(int(rng.integers(2, 6))):
        boxes.append((BUOY_CLASS, _draw_buoy(rng, img)))
    # 再画袋（目标类，压在浮标之上更接近真实水面漂浮）
    for _ in range(int(rng.integers(1, 3))):
        boxes.append((BAG_CLASS, _draw_bag(rng, img)))

    return img, boxes


def _to_yolo(cls: str, xyxy: tuple[int, int, int, int]) -> Box:
    x1, y1, x2, y2 = xyxy
    w = (x2 - x1) / FRAME_W
    h = (y2 - y1) / FRAME_H
    cx = ((x1 + x2) / 2) / FRAME_W
    cy = ((y1 + y2) / 2) / FRAME_H
    return Box(cls, cx, cy, w, h, 1.0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成合成塑料袋/浮标数据集")
    ap.add_argument("--config", default=None, help="配置文件（默认 edge/grasp/config.yaml）")
    ap.add_argument("--count", type=int, default=160, help="生成帧数")
    ap.add_argument("--tag", default="synthetic_demo", help="采集会话标签")
    ap.add_argument("--seed", type=int, default=20261015)
    ap.add_argument("--build", action="store_true", help="生成后立即组装 train/val")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    dataset = Dataset.from_config(cfg)
    rng = np.random.default_rng(args.seed)

    session = dataset.new_session(args.tag)
    n_bag = n_buoy = 0
    for i in range(args.count):
        frame, boxes = synth_frame(rng)
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if not ok:
            continue
        path = dataset.add_frame(session, buf.tobytes())
        dataset.write_label(path, [_to_yolo(c, b) for c, b in boxes])
        n_bag += sum(1 for c, _ in boxes if c == BAG_CLASS)
        n_buoy += sum(1 for c, _ in boxes if c == BUOY_CLASS)

    print(f"[生成] 会话={session} 帧={args.count} "
          f"袋框={n_bag} 浮标框={n_buoy} → {dataset.raw_dir / session}")

    if args.build:
        stats = dataset.build()
        print(f"[组装] {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
