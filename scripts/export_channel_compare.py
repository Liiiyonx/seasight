#!/usr/bin/env python
"""导出前端「双通道对照」演示数据（定性对照，不含任何精度指标）。

产出 ``frontend/public/demo/vision-channel-compare.json``。

★ 先说清这是什么、不是什么
---------------------------
这不是"哪个检测器更准"的对决，也**不产出 precision / recall / F1**。
它回答的是一个架构问题：

> 两条检测通道各自在什么条件下有效？在同一张图上各看到什么？

为什么必须是"两个 regime"而不是一张图上摆两列
----------------------------------------------
本轮实测（见 ``artifacts/metrics/vision_backends_latest.json`` 的
``threshold_probe`` 与两组结果）已经证明：**只摆一列会造成误读**。

1. **单张 4K 照片**：cv 通道恒为 0 —— 不是失效，是**适用范围之外**。
   ``CvDetector`` 是固定机位背景建模，靠"与背景的差异"找目标，
   单张照片没有可用的背景模型（整幅画面都会被判成一个巨型前景区域，
   再被 ``max_area`` 过滤）。world 通道零样本，单帧即可。
2. **连续帧（合成序列）**：cv 通道正常工作（172 条 / 93.3% 合成命中）；
   world 通道原始输出为 0 —— 合成帧是程序化绘制的纯色圆盘，
   缺少零样本模型赖以判别的外观纹理，属其输入分布之外。

只给一列，读数的人会把"不适用"读成"更差"。
所以本文件把两个 regime 都带上，前端才能把"两条通道回答不同问题"讲完整。

数据来源（★ 全部来自实测，没有手写数字）
----------------------------------------
- 静态图 regime 的 world 列：**直接复用** ``frontend/public/demo/*-detections.json``
  （已有的已验证样例数据，imgsz=1920）。重跑会因 imgsz 不同产生另一组数字，
  与单通道页面对不上，反而制造矛盾。
- 静态图 regime 的 cv 列：本脚本现场实测（同一套 ``CvDetector`` 与 config）。
- 视频流 regime：复用 ``scripts/compare_vision_backends.py`` 的
  ``run_cv_sequence`` / ``run_world_sequence`` —— 同一套代码，避免两处演化。

用法
----
    python scripts/export_channel_compare.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

# 复用对照脚本的通道构建与序列评测（同一套口径，不另抄一份）
import compare_vision_backends as cmp  # noqa: E402

DEMO_DIR = ROOT / "frontend" / "public" / "demo"
OUTPUT = DEMO_DIR / "vision-channel-compare.json"

# cv 通道静态预热帧数：与对照脚本同一口径
SYNTHETIC_FRAMES = 60

# 样例 id → 其已验证的 world 检测 JSON（单通道页面展示的就是这一份）
SAMPLE_WORLD_RESULTS = {
    "plastic-bottle": "beach-mixed-debris-detections.json",
    "discarded-shoes": "midway-shoes-detections.json",
    "fishing-gear": "fishing-net-detections.json",
    "polystyrene-cup": "styrofoam-cup-detections.json",
}


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
    return {"source": source, "value": value}


def load_world_sample(name: str) -> dict[str, Any]:
    """读取前端已有的 world 检测 JSON（不重跑，保证与单通道页面一致）。"""
    payload = json.loads((DEMO_DIR / name).read_text(encoding="utf-8"))
    items = payload.get("items") or []
    if not items or not isinstance(items[0].get("detections"), list):
        raise SystemExit(f"{name} 缺少 items[0].detections，样例数据不完整")
    item = items[0]
    return {
        "source_file": name,
        "model": payload.get("model"),
        "prompt_classes": payload.get("prompt_classes") or [],
        "conf": payload.get("confidence_threshold"),
        "iou": payload.get("iou_threshold"),
        "imgsz": payload.get("image_size"),
        "width": item.get("width"),
        "height": item.get("height"),
        "detections": item.get("detections"),
        "count": item.get("count"),
    }


def main() -> int:
    cfg = cmp.load_detector_config()

    world_paths = sorted(DEMO_DIR.glob("*-4k.webp"))
    if not world_paths:
        raise SystemExit(f"找不到演示图：{DEMO_DIR}")

    # ---- regime A：单张照片 ----
    # cv 列现场实测（预期 0 条，原因写进 caveat，而不是藏在数字后面）
    cv_stills = cmp.run_cv_stills(world_paths, cfg)
    per_image = {Path(r["name"]).name if "name" in r else "": r for r in cv_stills["per_image"]}

    still_images: list[dict[str, Any]] = []
    for path in world_paths:
        sample_id = next(
            (sid for sid, fn in SAMPLE_WORLD_RESULTS.items()
             if fn == path.name.replace("-4k.webp", "-detections.json")),
            None,
        )
        if sample_id is None:
            continue
        world = load_world_sample(SAMPLE_WORLD_RESULTS[sample_id])
        cv_row = per_image.get(path.name, {})
        still_images.append(
            {
                "sample_id": sample_id,
                "image": f"demo/{path.name}",
                "name": path.name,
                "width": world["width"],
                "height": world["height"],
                "cv": {
                    "detections": [],
                    "count": cv_row.get("count"),
                    "max_confidence": cv_row.get("max_confidence"),
                    "latency_ms": cv_row.get("latency_ms"),
                    "status": "evaluated",
                },
                "world": {
                    "detections": world["detections"],
                    "count": world["count"],
                    "max_confidence": max(
                        (float(d.get("confidence", 0.0)) for d in world["detections"]),
                        default=0.0,
                    ),
                    "prompt_classes": world["prompt_classes"],
                    "conf": world["conf"],
                    "imgsz": world["imgsz"],
                    "model": world["model"],
                    "source_file": world["source_file"],
                    "status": "verified_sample",
                },
            }
        )

    # ---- regime B：连续帧（合成序列） ----
    seq_images, seq_truths = cmp.build_synthetic_sequence(SYNTHETIC_FRAMES)
    cv_seq = cmp.run_cv_sequence(seq_images, seq_truths, cfg)
    world_seq = cmp.run_world_sequence(seq_images, seq_truths, cfg, [])

    video_stream = {
        "frames": SYNTHETIC_FRAMES,
        "note": "合成海面序列，真值为生成器 3 个泡沫团的外接框（程序化）",
        "evidence_level": "E1/E2",
        "not_generalizable": cmp.SYNTHETIC_EVIDENCE_NOTE,
        "cv": {
            "detections_total": cv_seq.get("detections_total"),
            "frames_with_detection": cv_seq.get("frames_with_detection"),
            "gt_targets_hit": cv_seq.get("gt_targets_hit"),
            "gt_targets_total": cv_seq.get("gt_targets_total"),
            "gt_hit_rate": cv_seq.get("gt_hit_rate"),
            "latency_ms": cv_seq.get("latency_ms", {}).get("mean"),
        },
        "world": {
            "detections_total": world_seq.get("detections_total"),
            "frames_with_detection": world_seq.get("frames_with_detection"),
            "gt_targets_hit": world_seq.get("gt_targets_hit"),
            "gt_targets_total": world_seq.get("gt_targets_total"),
            "latency_ms": world_seq.get("latency_ms", {}).get("mean"),
            "status": world_seq.get("status"),
            "reason": world_seq.get("reason"),
            # 0 检出必须带成因，否则会被读成"该通道无效"（ADR-027）
            "threshold_probe": world_seq.get("threshold_probe"),
            "caveats": world_seq.get("caveats") or [],
        },
    }

    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "kind": "qualitative_channel_comparison",
        "not_precision_recall": (
            "本文件不含真实数据集的 precision/recall/F1。独立测试集清单 status=planned、"
            "真值标注为 0，这些指标的分母为零。它只回答"
            "『两条通道各自在什么条件下有效、在同一张图上各看到什么』。"
        ),
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "code_version": code_version(),
        "channels": {
            "cv": {
                "backend": "cv",
                "label": "传统视觉",
                "engine": "OpenCV 背景建模 ∪ 颜色通道",
                "min_confidence": 0.45,
                "regime": "固定机位 + 连续帧（靠帧间差异）",
                "strength": "零数据依赖、可解释、逐帧确定性",
                "limitation": "需要连续帧建立背景模型；单张照片不在其适用范围内",
            },
            "world": {
                "backend": "world",
                "label": "开放词汇",
                "engine": "YOLO-World v2（零样本）+ CLIP 文本编码器",
                "min_confidence": float(_world_conf(cfg)),
                "regime": "单帧即可（靠语义）",
                "strength": "现场加词即可新增类别，无需采集、标注、重训",
                "limitation": "依赖外观纹理；置信度尺度与 cv 不同，不可横向比较",
            },
        },
        "regimes": {
            "still_image": {
                "title": "单张照片",
                "note": (
                    "cv 通道在本组恒为 0：它是固定机位背景建模，单张照片没有可用的"
                    "背景模型，这是适用范围之外，不是失效 —— 它在连续帧上正常工作"
                    "（见 video_stream 组）。"
                ),
                "images": still_images,
            },
            "video_stream": video_stream,
        },
        "caveats": [
            "两条通道的置信度尺度不同（cv 为 0.30~0.95 的伪置信度；world 低一个量级），"
            "分数不可横向比较，也不能放在一起排名。",
            "本文件是定性对照，不含精度、召回或可用性结论；独立测试集仍为空。",
            "video_stream 组的命中率是**合成数据口径（E1/E2）**，严禁外推为真实海域表现。",
            # 2026-09-27 实测：OpenCV 背景建模的内部状态在实例间不逐位可复现，
            # 短窗口的原始检出条数会有约 ±2 的运行间波动（逐帧比对首次分歧在第 34 帧；
            # 单线程也无法消除，排除线程调度）。本文件里的条数请按该粒度读。
            "cv 通道的原始检出条数在短窗口上有约 ±2 的运行间波动"
            "（OpenCV 背景建模内部状态所致）；跨运行的条数差异在该粒度内不算发现。",
        ],
    }

    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 控制台摘要 ----
    print(f"已写入 {OUTPUT}")
    print(f"\n[regime A] 单张照片（{len(still_images)} 张）")
    for row in still_images:
        print(
            f"  {row['name']:34s} cv={row['cv']['count']:>2} 条  "
            f"world={row['world']['count']:>2} 条  "
            f"(world prompts: {', '.join(row['world']['prompt_classes'])})"
        )
    print(f"\n[regime B] 连续帧（{SYNTHETIC_FRAMES} 帧合成海面）")
    print(
        f"  cv    = {cv_seq.get('detections_total')} 条 / "
        f"{cv_seq.get('frames_with_detection')} 帧有检出 / "
        f"合成命中 {cv_seq.get('gt_targets_hit')}/{cv_seq.get('gt_targets_total')}"
    )
    print(f"  world = {world_seq.get('detections_total')} 条 / "
          f"{world_seq.get('frames_with_detection')} 帧有检出")
    probe = world_seq.get("threshold_probe") or {}
    if probe:
        print(f"  world 诊断：conf 压到 {probe.get('probe_conf')} 重跑 raw={probe.get('raw')}")
    return 0


def _world_conf(cfg: dict[str, Any]) -> float:
    world_cfg = cfg.get("world") or {}
    if "conf" in world_cfg:
        return float(world_cfg["conf"])
    from world_detector import DEFAULT_WORLD_CONFIG  # noqa: PLC0415

    return float(DEFAULT_WORLD_CONFIG["conf"])


if __name__ == "__main__":
    raise SystemExit(main())
