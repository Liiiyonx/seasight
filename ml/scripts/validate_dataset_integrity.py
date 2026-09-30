#!/usr/bin/env python3
"""探海灵眸 SeaSight — 感知数据集完整性校验入口（WP-13）。

包装 `convert_annotations.validate_dataset`，提供独立 CLI：

  - 校验结构（12 个顶层字段、schema_version、图片/标注必填字段）
  - 校验图片路径、类别、bbox、面积、重复 ID、引用完整
  - 跨 split 泄漏（单文件内 train|val × test|blind；多文件输入时跨文件检测）
  - 校验和（sha256）一致性
  - 证据等级纪律（合成最高 E2；real 需真实来源+复核；E4 需凭证，不自动授予）

坏样本不删除，进入报告的 skipped_samples 并计数；status=invalid 时退出码 1。

能力口径（冻结）：确定性数据准备工具，不是 YOLO、不是视觉大模型、不是已训练模型；
没有样本时输出 evaluation_status=not_evaluated，不生成任何虚假指标。

退出码：
  0 = 有效（无阻断问题；坏样本为 0 或 --strict 未触发）
  1 = 无效（结构/证据/坏样本/泄漏）
  2 = 输入文件缺失或不可读
  3 = 依赖缺失
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 与 convert_annotations.py 同目录，直接复用其校验核心（冻结常量唯一来源）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from convert_annotations import (  # noqa: E402
    ConfigError,
    SCHEMA_VERSION,
    load_json,
    sha256_file,
    validate_dataset,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="validate_dataset_integrity",
        description="SeaSight COCO-like JSON 数据集完整性校验（WP-13）",
    )
    ap.add_argument("--input", action="append", required=True,
                    help="SeaSight JSON 路径；可多次指定以做跨文件泄漏检测")
    ap.add_argument("--images-root", default=None, help="数据集根目录（校验图片路径/校验和）")
    ap.add_argument("--no-check-files", action="store_true")
    ap.add_argument("--no-check-checksums", action="store_true")
    ap.add_argument("--verify-dimensions", action="store_true")
    ap.add_argument("--strict", action="store_true", help="有警告也返回 1")
    ap.add_argument("--report", default=None, help="合并报告 JSON 输出路径")
    args = ap.parse_args(argv)

    reports: list[dict] = []
    for src in args.input:
        try:
            data, h = load_json(src)
        except ConfigError as e:
            print(f"[错误] {e}", file=sys.stderr)
            return 2
        rep = validate_dataset(
            data,
            source_path=src,
            images_root=args.images_root,
            check_files=not args.no_check_files,
            check_checksums=not args.no_check_checksums,
            verify_dimensions=args.verify_dimensions,
        )
        reports.append(rep)
        _print_one(rep, src)

    # 跨文件泄漏：train|val 与 test|blind 不得出现同一 file_name
    cross = _cross_file_leakage(reports, args.input)
    for msg in cross:
        print(f"✗ {msg}", file=sys.stderr)
    if cross:
        reports[0]["problems"] += ["跨文件泄漏：" + m for m in cross]
        reports[0]["leakage"] = reports[0].get("leakage", []) + cross
        reports[0]["isolation_violation"] = True

    invalid = any(r["status"] == "invalid" for r in reports)
    warnings = sum(len(r["warnings"]) for r in reports)
    merged = {
        "status": "invalid" if invalid or (args.strict and warnings) else "valid",
        "schema_version": SCHEMA_VERSION,
        "inputs": [
            {"path": s, "sha256": sha256_file(Path(s))}
            for s in args.input
        ],
        "reports": reports,
        "cross_file_leakage": cross,
    }
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"合并报告已写入: {args.report}")

    print("=" * 56)
    if merged["status"] == "valid":
        print(f"✓ 全部输入有效（警告 {warnings} 条）")
        return 0
    print(f"✗ 存在阻断性问题（{sum(1 for r in reports if r['status']=='invalid')} 个输入无效）")
    return 1


def _cross_file_leakage(reports: list[dict], inputs: list[str]) -> list[str]:
    """跨文件泄漏检测：需要原始数据里的 split，这里直接重读输入文件。"""
    low_names: dict[str, str] = {}
    high_names: dict[str, str] = {}
    low_ids: dict[int, str] = {}
    high_ids: dict[int, str] = {}
    for src in inputs:
        try:
            data, _ = load_json(src)
        except ConfigError:
            continue
        for img in data.get("images", []):
            split = img.get("split")
            name = str(img.get("file_name", ""))
            iid = img.get("id")
            if split in ("train", "val"):
                low_names.setdefault(name, src)
                if isinstance(iid, int):
                    low_ids.setdefault(iid, src)
            elif split in ("test", "blind"):
                high_names.setdefault(name, src)
                if isinstance(iid, int):
                    high_ids.setdefault(iid, src)
    out: list[str] = []
    for name in sorted(set(low_names) & set(high_names)):
        out.append(f"file_name {name!r} 同时出现在 {low_names[name]} 与 {high_names[name]}")
    for iid in sorted(set(low_ids) & set(high_ids)):
        out.append(f"image id={iid} 同时出现在 {low_ids[iid]} 与 {high_ids[iid]}")
    return out


def _print_one(report: dict, src: str) -> None:
    print(f"── {src}")
    print(f"  dataset_id      : {report.get('dataset_id')}")
    print(f"  dataset_type    : {report.get('dataset_type')}")
    print(f"  evidence_level  : {report.get('evidence_level')}")
    print(f"  样本数          : images={report['sample_count']['images']} "
          f"annotations={report['sample_count']['annotations']}")
    dist = report.get("category_distribution") or {}
    print(f"  类别分布        : {dist if dist else '（无）'}")
    print(f"  输入文件哈希    : {[f['sha256'][:12] for f in report.get('input_files', [])]}")
    skipped = report.get("skipped_samples") or []
    print(f"  跳过的坏样本    : {len(skipped)}")
    for s in skipped[:8]:
        print(f"    - [{s['type']}] id={s.get('id')}  {s.get('reason')}")
    if len(skipped) > 8:
        print(f"    ... 另 {len(skipped) - 8} 条")
    if report.get("isolation_violation"):
        print(f"  ✗ 跨 split 泄漏：{report.get('leakage')}")
    for w in report.get("warnings", [])[:5]:
        print(f"  ⚠ {w}")
    if report["status"] == "invalid":
        for p in report.get("problems", [])[:8]:
            print(f"  ✗ {p}")
        print(f"  状态            : invalid")
    else:
        print(f"  状态            : valid（evaluation_status="
              f"{report.get('evaluation_status')}）")


if __name__ == "__main__":
    raise SystemExit(main())
