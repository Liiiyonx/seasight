#!/usr/bin/env python3
"""探海灵眸 Oceanus — 感知数据标注转换与完整性校验核心（WP-13）。

冻结格式：Oceanus COCO-like JSON（schema_version "1.0"），详见
`docs/perception-data-protocol.md`。本模块提供：

  1. `validate_dataset`  —— 协议级完整性校验（结构、图片路径、类别、bbox、
                          面积、重复 ID、引用完整、跨 split 泄漏、校验和、
                          证据等级纪律），坏样本跳过并列入报告。
  2. `seasight_to_yolo`  —— Oceanus JSON → YOLO txt（每行 class cx cy w h 归一化）。
  3. `yolo_to_seasight`  —— YOLO txt 目录 → Oceanus JSON（明确导入入口，
                          标注 source_type="imported"、review_status="unreviewed"）。

能力口径（冻结）：
  - 本脚本是确定性数据准备工具，不是 YOLO、不是视觉大模型、不是已训练深度模型。
  - 合成数据最高 E2；real 默认 E3 且必须带真实采集来源与复核状态；
    E4 需要显式 `evidence_upgrade`（合同/订单/验收凭证），脚本绝不自动授予。
  - 没有样本时只准备工具/协议，不生成任何虚假精度指标。

退出码（CLI）：
  0 = 成功（validate=有效；to-yolo/from-yolo=已产出）
  1 = 无效 / 转换失败 / --strict 下有坏样本被跳过
  2 = 输入文件缺失或参数错误
  3 = 依赖缺失（from-yolo 需要 Pillow 读取图片尺寸）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ----------------------------------------------------------------------
# 冻结接口常量（与 docs/perception-data-protocol.md 一一对应）
# ----------------------------------------------------------------------

SCHEMA_VERSION = "1.0"

REQUIRED_TOP_LEVEL = (
    "schema_version",
    "dataset_id",
    "dataset_type",
    "evidence_level",
    "source",
    "captured_at",
    "camera",
    "images",
    "annotations",
    "categories",
    "calibration",
    "checksums",
)

REQUIRED_ANNOTATION_FIELDS = (
    "image_id",
    "category_id",
    "bbox",
    "area",
    "iscrowd",
    "source_type",
    "review_status",
)

DATASET_TYPES = ("planned", "synthetic", "quasi_real", "real")
SOURCE_TYPES = ("manual", "imported", "synthetic", "quasi_real", "real")
REVIEW_STATUSES = ("unreviewed", "reviewed", "approved")
SPLITS = ("train", "val", "test", "blind")

# evidence_level 上限（与 evaluate_opencv.ALLOWED_EVIDENCE 一致；E4 另有门禁）
ALLOWED_EVIDENCE: dict[str, tuple[str, ...]] = {
    "planned": ("E0",),
    "synthetic": ("E1", "E2"),
    "quasi_real": ("E2",),
    "real": ("E3", "E4"),  # E4 必须显式 evidence_upgrade，见 _check_evidence
}

EVIDENCE_ORDER = {"E0": 0, "E1": 1, "E2": 2, "E3": 3, "E4": 4}

IMG_SUFFIX = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# 面积 / bbox 数值容差
TOL = 1e-3

THIS_FILE = Path(__file__).resolve()
ML = THIS_FILE.parent.parent          # ml/
ROOT = ML.parent                      # 仓库根

CODE_TAG = "seasight-wp13-20260919"   # 无 .git 时的固定版本串


class ConfigError(Exception):
    """配置或输入错误。"""


class DependencyError(Exception):
    """依赖缺失。"""


# ----------------------------------------------------------------------
# 哈希与 IO
# ----------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path) -> tuple[dict[str, Any], str]:
    """读取 JSON，返回 (data, sha256)。文件缺失/损坏抛 ConfigError。"""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"找不到输入文件 {p}")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"无法读取 {p}: {e}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{p} 不是合法 JSON: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{p} 顶层必须是 JSON 对象")
    return data, sha256_bytes(text.encode("utf-8"))


# ----------------------------------------------------------------------
# 证据等级纪律
# ----------------------------------------------------------------------

def _resolve_evidence(dataset_type: str, evidence_level: str | None) -> str:
    if dataset_type not in DATASET_TYPES:
        raise ConfigError(f"未知 dataset_type={dataset_type!r}，允许 {DATASET_TYPES}")
    allowed = ALLOWED_EVIDENCE[dataset_type]
    if evidence_level is None:
        return allowed[0]
    if evidence_level not in allowed:
        raise ConfigError(
            f"evidence_level={evidence_level!r} 超出 {dataset_type} 允许范围 {allowed}"
            "（冻结口径：合成数据最高 E2，不得写成 E3/E4；real 默认 E3）"
        )
    return evidence_level


def _check_evidence(
    dataset_type: str, evidence_level: str, data: dict[str, Any], problems: list[str]
) -> None:
    """证据等级纪律（冻结）：
    - dataset_type 必须合法且 evidence_level 在其允许范围；
    - synthetic 数据集禁止 E3/E4；
    - real 必须带真实采集来源（source.acquisition_note 或 device/region）；
    - E4 只有显式 evidence_upgrade（合同/订单/验收凭证）才被接受，脚本不自动授予。
    """
    if dataset_type not in DATASET_TYPES:
        problems.append(f"dataset_type={dataset_type!r} 非法，允许 {DATASET_TYPES}")
        return
    allowed = ALLOWED_EVIDENCE[dataset_type]
    if evidence_level not in allowed:
        problems.append(
            f"evidence_level={evidence_level!r} 超出 {dataset_type} 允许范围 {allowed}"
            "（合成数据最高 E2；脚本不自动授予 E4）"
        )
        return
    if dataset_type == "real":
        src = data.get("source") or {}
        has_note = str(src.get("acquisition_note", "")).strip()
        has_dev = str(src.get("device", "")).strip()
        has_region = str(src.get("region", "")).strip()
        if not (has_note or has_dev or has_region):
            problems.append(
                "real 数据集必须带真实采集来源（source.acquisition_note / device / region）"
            )
    if evidence_level == "E4":
        upgrade = data.get("evidence_upgrade")
        basis = (upgrade or {}).get("basis", "") if isinstance(upgrade, dict) else ""
        reference = str((upgrade or {}).get("reference", "")) if isinstance(upgrade, dict) else ""
        if basis not in ("contract", "order", "acceptance") or not reference.strip():
            problems.append(
                "E4 需要显式 evidence_upgrade={basis: contract|order|acceptance, reference: ...}；"
                "脚本绝不自动授予 E4"
            )


# ----------------------------------------------------------------------
# 索引构建
# ----------------------------------------------------------------------

def _build_indexes(data: dict[str, Any]) -> tuple[dict[int, dict], dict[int, dict], dict[str, list[int]]]:
    images_by_id: dict[int, dict] = {}
    images_by_name: dict[str, list[int]] = {}
    categories_by_id: dict[int, dict] = {}
    for img in data.get("images", []):
        iid = img.get("id")
        if isinstance(iid, int):
            images_by_id[iid] = img
        name = str(img.get("file_name", ""))
        images_by_name.setdefault(name, []).append(iid)
    for cat in data.get("categories", []):
        cid = cat.get("id")
        if isinstance(cid, int):
            categories_by_id[cid] = cat
    return images_by_id, categories_by_id, images_by_name


def category_distribution(data: dict[str, Any]) -> dict[str, int]:
    """{类别名: 实例数}。"""
    cats = {c["id"]: c.get("name", str(c["id"])) for c in data.get("categories", []) if "id" in c}
    dist: Counter[str] = Counter()
    for ann in data.get("annotations", []):
        cid = ann.get("category_id")
        if cid in cats:
            dist[cats[cid]] += 1
    return dict(sorted(dist.items()))


# ----------------------------------------------------------------------
# 单条校验
# ----------------------------------------------------------------------

def _check_bbox(ann: dict[str, Any], img: dict[str, Any] | None) -> list[str]:
    errs: list[str] = []
    bbox = ann.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        errs.append("bbox 必须是 [x, y, w, h] 四个数值")
        return errs
    try:
        x, y, w, h = (float(v) for v in bbox)
    except (TypeError, ValueError):
        errs.append("bbox 含非数值字段")
        return errs
    if x < -TOL or y < -TOL:
        errs.append(f"bbox 左上角不能为负: {bbox}")
    if w <= 0 or h <= 0:
        errs.append(f"bbox 宽高必须为正: {bbox}")
    if img is not None:
        W, H = img.get("width"), img.get("height")
        if isinstance(W, (int, float)) and isinstance(H, (int, float)) and W > 0 and H > 0:
            if x + w > W + TOL or y + h > H + TOL:
                errs.append(f"bbox 超出图片尺寸 [{W}x{H}]: {bbox}")
    area = ann.get("area")
    if isinstance(area, (int, float)) and w > 0 and h > 0:
        expect = w * h
        if abs(float(area) - expect) > max(TOL, TOL * expect):
            errs.append(f"area={area} 与 bbox 面积 {expect:.4f} 不一致")
    elif not isinstance(area, (int, float)):
        errs.append("area 必须是数值")
    return errs


def validate_annotation(
    ann: dict[str, Any],
    images_by_id: dict[int, dict],
    categories_by_id: dict[int, dict],
    dataset_type: str,
    evidence_level: str,
) -> list[str]:
    """单条标注校验，返回问题列表；非空即坏样本。"""
    errs: list[str] = []
    for f in REQUIRED_ANNOTATION_FIELDS:
        if f not in ann or ann[f] is None:
            errs.append(f"缺少必填字段 {f}")
            return errs  # 字段都不全，后续检查无意义
    iid = ann.get("image_id")
    img = images_by_id.get(iid) if isinstance(iid, int) else None
    if img is None:
        errs.append(f"image_id={iid!r} 在 images[] 中不存在")
    cid = ann.get("category_id")
    if cid not in categories_by_id:
        errs.append(f"category_id={cid!r} 在 categories[] 中不存在")
    errs.extend(_check_bbox(ann, img))
    st = ann.get("source_type")
    if st not in SOURCE_TYPES:
        errs.append(f"source_type={st!r} 非法，允许 {SOURCE_TYPES}")
    rs = ann.get("review_status")
    if rs not in REVIEW_STATUSES:
        errs.append(f"review_status={rs!r} 非法，允许 {REVIEW_STATUSES}")
    iscrowd = ann.get("iscrowd")
    if iscrowd not in (0, 1):
        errs.append(f"iscrowd 必须是 0 或 1，实际 {iscrowd!r}")
    # 证据一致性
    if st == "synthetic" and EVIDENCE_ORDER.get(evidence_level, 0) >= 3:
        errs.append(
            f"合成标注（source_type=synthetic）出现在 E{evidence_level} 数据集中："
            "合成数据最高 E2，禁止抬高真实数据集证据"
        )
    if st == "real" and rs not in ("reviewed", "approved"):
        errs.append("real 标注必须带复核状态 review_status=reviewed|approved")
    if st == "real" and dataset_type != "real":
        errs.append("real 标注只能出现在 dataset_type=real 的数据集中")
    return errs


def _check_image(
    img: dict[str, Any],
    seen_ids: set[int],
    seen_names: dict[str, int],
    images_root: Path | None,
    checksum_values: dict[str, str],
    check_files: bool,
    check_checksums: bool,
) -> list[str]:
    errs: list[str] = []
    for f in ("id", "file_name", "width", "height", "split"):
        if f not in img or img[f] is None:
            errs.append(f"缺少必填字段 {f}")
    iid = img.get("id")
    if isinstance(iid, int):
        if iid in seen_ids:
            errs.append(f"image id={iid} 重复")
        seen_ids.add(iid)
    name = str(img.get("file_name", ""))
    if name:
        if seen_names.get(name):
            errs.append(f"file_name 重复: {name}")
        seen_names[name] = iid
    split = img.get("split")
    if split not in SPLITS:
        errs.append(f"split={split!r} 非法，允许 {SPLITS}")
    W, H = img.get("width"), img.get("height")
    if not (isinstance(W, (int, float)) and isinstance(H, (int, float)) and W > 0 and H > 0):
        errs.append("width/height 必须是正数")
    if check_files and images_root is not None:
        fpath = images_root / name
        if not fpath.is_file():
            errs.append(f"图片文件不存在: {name}")
    if check_checksums and name and name in checksum_values:
        if images_root is not None:
            fpath = images_root / name
            if fpath.is_file():
                actual = sha256_file(fpath)
                if actual != checksum_values[name]:
                    errs.append(f"校验和不一致: {name}")
            else:
                errs.append(f"无法校验（文件不存在）: {name}")
    return errs


def detect_split_leakage(images: list[dict[str, Any]]) -> list[str]:
    """跨 split 泄漏：同一 file_name / 同一 image id 同时出现在
    train|val 与 test|blind → 泄漏（冻结口径见 manifests/README.md）。"""
    by_name: dict[str, set[str]] = {}
    by_id: dict[int, set[str]] = {}
    for img in images:
        split = img.get("split")
        if split not in SPLITS:
            continue
        name = str(img.get("file_name", ""))
        by_name.setdefault(name, set()).add(split)
        iid = img.get("id")
        if isinstance(iid, int):
            by_id.setdefault(iid, set()).add(split)
    violations: list[str] = []
    for name, splits in by_name.items():
        low = splits & {"train", "val"}
        high = splits & {"test", "blind"}
        if low and high:
            violations.append(f"file_name {name!r} 同时出现在 {sorted(low)} 与 {sorted(high)}")
    for iid, splits in by_id.items():
        low = splits & {"train", "val"}
        high = splits & {"test", "blind"}
        if low and high:
            violations.append(f"image id={iid} 同时出现在 {sorted(low)} 与 {sorted(high)}")
    return violations


# ----------------------------------------------------------------------
# 数据集级校验（返回报告 dict）
# ----------------------------------------------------------------------

def validate_dataset(
    data: dict[str, Any],
    source_path: str | Path | None = None,
    images_root: str | Path | None = None,
    check_files: bool = True,
    check_checksums: bool = True,
    verify_dimensions: bool = False,
) -> dict[str, Any]:
    """完整校验一份 Oceanus COCO-like JSON，返回报告。

    坏样本（问题标注/图片）不删除数据，而是进入 skipped_samples 并计数，
    调用方根据 status 决定是否放行。
    """
    problems: list[str] = []
    warnings: list[str] = []
    skipped: list[dict[str, Any]] = []

    for f in REQUIRED_TOP_LEVEL:
        if f not in data:
            problems.append(f"缺少必填顶层字段 {f}")
    if problems:
        return _mk_report(data, problems, warnings, skipped, source_path)

    if data.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version 应为 {SCHEMA_VERSION}，实际 {data.get('schema_version')!r}")
    dataset_type = data.get("dataset_type")
    evidence_level = data.get("evidence_level")
    _check_evidence(dataset_type, evidence_level, data, problems)

    images = data.get("images")
    annotations = data.get("annotations")
    if not isinstance(images, list):
        problems.append("images 必须是列表")
        images = []
    if not isinstance(annotations, list):
        problems.append("annotations 必须是列表")
        annotations = []
    if dataset_type == "planned" and (images or annotations):
        problems.append("planned 数据集不允许包含图片或标注（只有规划，没有数据）")

    images_by_id, categories_by_id, images_by_name = _build_indexes(data)
    checksums = data.get("checksums") or {}
    checksum_values = checksums.get("values", {}) if isinstance(checksums, dict) else {}
    if isinstance(checksums, dict) and checksums.get("algorithm") not in (None, "sha256"):
        warnings.append(f"checksums.algorithm={checksums.get('algorithm')!r} 非 sha256，跳过校验")

    root = Path(images_root).resolve() if images_root else None

    seen_ids: set[int] = set()
    seen_names: dict[str, int] = {}
    for img in images:
        errs = _check_image(
            img, seen_ids, seen_names, root, checksum_values,
            check_files=check_files, check_checksums=check_checksums,
        )
        if errs:
            skipped.append({"type": "image", "id": img.get("id"), "reason": "；".join(errs)})
        elif verify_dimensions and root is not None:
            name = str(img.get("file_name", ""))
            fpath = root / name
            if fpath.is_file():
                dims = _image_size(fpath)
                if dims and (dims != (img.get("width"), img.get("height"))):
                    warnings.append(
                        f"图片尺寸与记录不符: {name} 磁盘 {dims} vs 记录 "
                        f"[{img.get('width')}x{img.get('height')}]"
                    )
        # 图片级：checksum 缺失记警告（不阻断）
        name = str(img.get("file_name", ""))
        if name and check_checksums and name not in checksum_values:
            warnings.append(f"图片 {name} 缺少 checksums 记录")

    # 标注校验（自动补 id 以便去重）
    seen_ann_ids: set[int] = set()
    auto_id = max((a.get("id", 0) for a in annotations if isinstance(a.get("id"), int)), default=0)
    for ann in annotations:
        if ann.get("id") is None:
            auto_id += 1
            ann["id"] = auto_id
        aerrs = validate_annotation(ann, images_by_id, categories_by_id, dataset_type, evidence_level)
        if isinstance(ann.get("id"), int) and ann["id"] in seen_ann_ids:
            aerrs.append(f"annotation id={ann['id']} 重复")
        seen_ann_ids.add(ann["id"])
        if aerrs:
            skipped.append({"type": "annotation", "id": ann.get("id"), "reason": "；".join(aerrs)})

    violations = detect_split_leakage(images)
    report = _mk_report(data, problems, warnings, skipped, source_path)
    report["isolation_violation"] = bool(violations)
    report["leakage"] = violations
    if violations:
        report["problems"] += ["跨 split 泄漏：" + v for v in violations]
    return report


def _mk_report(
    data: dict[str, Any],
    problems: list[str],
    warnings: list[str],
    skipped: list[dict[str, Any]],
    source_path: str | Path | None,
) -> dict[str, Any]:
    images = data.get("images", []) or []
    annotations = data.get("annotations", []) or []
    n_img = len(images)
    n_ann = len(annotations)
    status = "invalid" if problems or skipped else "valid"
    report: dict[str, Any] = {
        "status": status,
        "evaluation_status": "not_evaluated" if n_img == 0 else "prepared",
        "schema_version": data.get("schema_version"),
        "dataset_id": data.get("dataset_id"),
        "dataset_type": data.get("dataset_type"),
        "evidence_level": data.get("evidence_level"),
        "code_tag": CODE_TAG,
        "input_files": [],
        "sample_count": {"images": n_img, "annotations": n_ann},
        "category_distribution": category_distribution(data),
        "skipped_samples": skipped,
        "problems": problems,
        "warnings": warnings,
        "isolation_violation": False,
        "leakage": [],
    }
    if source_path is not None:
        p = Path(source_path)
        report["input_files"].append({"path": str(p), "sha256": sha256_file(p) if p.is_file() else ""})
    return report


def _image_size(path: Path) -> tuple[int, int] | None:
    """用 Pillow 读图片尺寸；失败返回 None。"""
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            return im.width, im.height
    except Exception:
        return None


# ----------------------------------------------------------------------
# Oceanus JSON → YOLO txt
# ----------------------------------------------------------------------

def seasight_to_yolo(
    data: dict[str, Any],
    images_root: str | Path,
    output_root: str | Path,
    strict: bool = False,
    source_path: str | Path | None = None,
) -> dict[str, Any]:
    """把 Oceanus JSON 的标注导出为 YOLO txt（class cx cy w h，归一化 0~1）。

    - 只导出校验通过的标注（坏样本跳过并列入报告）。
    - 负样本图片（无有效标注）不生成标签文件，报告计数。
    """
    report = validate_dataset(
        data, source_path=source_path, check_files=False, check_checksums=False
    )
    images_root_p = Path(images_root)
    out_root = Path(output_root)
    out_root.mkdir(parents=True, exist_ok=True)

    good_ann_ids = {s.get("id") for s in report["skipped_samples"] if s.get("type") == "annotation"}
    good_img_ids = {s.get("id") for s in report["skipped_samples"] if s.get("type") == "image"}

    categories = {c["id"]: c.get("name", str(c["id"])) for c in data.get("categories", []) if "id" in c}
    images_by_id = {img["id"]: img for img in data.get("images", []) if isinstance(img.get("id"), int)}
    exported_images = 0
    negative_images = 0
    for img in data.get("images", []):
        iid = img.get("id")
        if iid in good_img_ids:
            continue
        name = str(img.get("file_name", ""))
        W, H = img.get("width"), img.get("height")
        if not (isinstance(W, (int, float)) and isinstance(H, (int, float)) and W > 0 and H > 0):
            report["warnings"].append(f"图片 {name} 尺寸非法，跳过导出")
            continue
        lines: list[str] = []
        for ann in data.get("annotations", []):
            if ann.get("image_id") != iid or ann.get("id") in good_ann_ids:
                continue
            x, y, w, h = (float(v) for v in ann["bbox"])
            cx = (x + w / 2.0) / W
            cy = (y + h / 2.0) / H
            wn = w / W
            hn = h / H
            if any(v < -1e-6 or v > 1.0 + 1e-6 for v in (cx, cy, wn, hn)):
                report["warnings"].append(
                    f"标注 id={ann.get('id')} 归一化越界（图片 {name}），跳过导出"
                )
                continue
            lines.append(f"{ann['category_id']} {cx:.6f} {cy:.6f} {wn:.6f} {hn:.6f}")
        split = img.get("split", "train")
        if split not in SPLITS:
            report["warnings"].append(f"图片 {name} split={split!r} 非法，跳过导出")
            continue
        if not lines:
            negative_images += 1
            continue
        lbl_dir = out_root / "labels" / split
        lbl_dir.mkdir(parents=True, exist_ok=True)
        (lbl_dir / (Path(name).stem + ".txt")).write_text("\n".join(lines) + "\n", encoding="utf-8")
        exported_images += 1

    report["status"] = "invalid" if report["problems"] or report["skipped_samples"] else "valid"
    if strict and report["skipped_samples"]:
        report["problems"].append("--strict：存在坏样本被跳过")
        report["status"] = "invalid"
    report["export"] = {
        "exported_images": exported_images,
        "negative_images": negative_images,
        "output_root": str(out_root),
        "labels": {cid: categories.get(cid, str(cid)) for cid in sorted(categories)},
    }
    return report


# ----------------------------------------------------------------------
# YOLO txt → Oceanus JSON（导入入口）
# ----------------------------------------------------------------------

def yolo_to_seasight(
    images_dir: str | Path,
    labels_dir: str | Path,
    dataset_type: str,
    evidence_level: str,
    output: str | Path,
    dataset_id: str | None = None,
    split: str = "train",
    categories: list[str] | None = None,
    source: dict[str, Any] | None = None,
    captured_at: str | None = None,
    camera: dict[str, Any] | None = None,
    check_files: bool = True,
    skip_image_hash: bool = False,
    contract_reference: str | None = None,
) -> dict[str, Any]:
    """从 YOLO txt 目录导入为 Oceanus COCO-like JSON。

    导入纪律（冻结）：
      - 所有标注 source_type="imported"、review_status="unreviewed"（须人工复核）；
      - 必须显式给出 dataset_type / evidence_level；越级直接报错；
      - real 必须提供 source（设备/区域/采集说明）。
    """
    img_dir = Path(images_dir)
    lbl_dir = Path(labels_dir)
    if not img_dir.is_dir():
        raise ConfigError(f"图片目录不存在: {img_dir}")
    if not lbl_dir.is_dir():
        raise ConfigError(f"标签目录不存在: {lbl_dir}")

    dataset_type = str(dataset_type)
    if dataset_type == "planned":
        raise ConfigError("planned 数据集不能导入图片（只有规划，没有数据）")
    evidence_level = _resolve_evidence(dataset_type, evidence_level)
    if evidence_level == "E4" and not contract_reference:
        raise ConfigError("E4 需要 --contract-reference（合同/订单/验收凭证编号），脚本不自动授予")
    if dataset_type == "real" and not source:
        raise ConfigError("real 数据集必须提供 --source-device / --source-region / --source-note")
    if split not in SPLITS:
        raise ConfigError(f"split 必须属于 {SPLITS}，实际 {split!r}")
    if not categories:
        categories = ["foam", "plastic", "fishing_gear", "other"]  # 与 seasight.yaml names 一致
    cats = [{"id": i, "name": n, "supercategory": "marine_litter"} for i, n in enumerate(categories)]
    cat_by_name = {c["name"]: c["id"] for c in cats}

    images: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[str] = []
    dist: Counter[int] = Counter()

    image_files = sorted(p for p in img_dir.rglob("*") if p.is_file() and p.suffix.lower() in IMG_SUFFIX)
    if not image_files:
        raise ConfigError(f"图片目录 {img_dir} 中没有可导入的图片（{sorted(IMG_SUFFIX)}）")

    ann_id = 0
    img_id = 0
    for p in image_files:
        rel = p.relative_to(img_dir)
        label_file = lbl_dir / rel.with_suffix(".txt")
        if not label_file.exists():
            # 同级目录平铺时也允许（labels 目录结构与 images 一致镜像）
            label_file = lbl_dir / (p.stem + ".txt")
        dims = _image_size(p)
        if dims is None:
            skipped.append({"type": "image", "id": None, "reason": f"无法读取图片尺寸: {rel}"})
            continue
        W, H = dims
        img_id += 1
        img_entry: dict[str, Any] = {
            "id": img_id,
            "file_name": str(rel),
            "width": W,
            "height": H,
            "split": split,
        }
        if not skip_image_hash:
            img_entry["sha256"] = sha256_file(p)
        images.append(img_entry)

        if not label_file.exists():
            warnings.append(f"图片 {rel} 无标签文件（按负样本处理）")
            continue
        try:
            lines = [ln.strip() for ln in label_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except OSError:
            skipped.append({"type": "label", "id": str(rel), "reason": f"标签文件不可读: {label_file}"})
            continue
        for no, ln in enumerate(lines, 1):
            parts = ln.split()
            if len(parts) != 5:
                skipped.append({"type": "label_line", "id": f"{rel}:{no}", "reason": f"字段数 {len(parts)} ≠ 5"})
                continue
            try:
                cid = int(float(parts[0]))
                vals = [float(v) for v in parts[1:]]
            except ValueError:
                skipped.append({"type": "label_line", "id": f"{rel}:{no}", "reason": "含非数字字段"})
                continue
            cx, cy, wn, hn = vals
            if cid not in cat_by_name.values():
                skipped.append({"type": "label_line", "id": f"{rel}:{no}", "reason": f"类别索引 {cid} 越界（0~{len(cats)-1}）"})
                continue
            if any(v < -TOL or v > 1.0 + TOL for v in vals):
                skipped.append({"type": "label_line", "id": f"{rel}:{no}", "reason": "坐标未归一化到 0~1"})
                continue
            x = (cx - wn / 2.0) * W
            y = (cy - hn / 2.0) * H
            w = wn * W
            h = hn * H
            x = max(0.0, x)
            y = max(0.0, y)
            ann_id += 1
            annotations.append({
                "id": ann_id,
                "image_id": img_id,
                "category_id": cid,
                "bbox": [round(x, 4), round(y, 4), round(w, 4), round(h, 4)],
                "area": round(w * h, 4),
                "iscrowd": 0,
                "source_type": "imported",
                "review_status": "unreviewed",
                "reviewer": "",
                "note": f"imported from {label_file.name} line {no}",
            })
            dist[cid] += 1

    if not images:
        raise ConfigError("没有可导入的图片（全部被跳过）")

    checksum_values = {}
    if not skip_image_hash:
        for im in images:
            checksum_values[str(im["file_name"])] = im["sha256"]

    dataset = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": dataset_id or f"imported_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}",
        "dataset_type": dataset_type,
        "evidence_level": evidence_level,
        "source": source or {"device": "", "region": "", "acquisition_note": ""},
        "captured_at": captured_at or datetime.now(timezone.utc).isoformat(),
        "camera": camera or {"model": "", "lens": "", "resolution": None, "calibration_ref": None},
        "images": images,
        "annotations": annotations,
        "categories": cats,
        "calibration": {
            "camera_matrix": None, "dist_coeffs": None, "homography": None,
            "square_size_m": None, "pattern": None, "ref": "",
        },
        "checksums": {"algorithm": "sha256", "values": checksum_values},
    }
    if evidence_level == "E4":
        dataset["evidence_upgrade"] = {
            "basis": "contract",
            "reference": contract_reference or "",
        }

    out_p = Path(output)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # 报告：输入标签文件哈希（YOLO txt 体积小，逐文件 sha256）
    input_files: list[dict[str, Any]] = [
        {"path": str(lp.relative_to(lbl_dir)), "sha256": sha256_file(lp)}
        for lp in sorted(lbl_dir.rglob("*.txt"))
    ]
    report: dict[str, Any] = {
        "status": "ok",
        "code_tag": CODE_TAG,
        "dataset_id": dataset["dataset_id"],
        "dataset_type": dataset_type,
        "evidence_level": evidence_level,
        "split": split,
        "input_files": input_files,
        "sample_count": {"images": len(images), "annotations": len(annotations)},
        "category_distribution": {categories[i]: dist[i] for i in sorted(dist)},
        "skipped_samples": skipped,
        "warnings": warnings,
        "problems": [],
        "output": str(out_p),
        "output_sha256": sha256_file(out_p),
    }
    return report


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def _print_report(report: dict[str, Any], title: str) -> None:
    print(f"==== {title} ====")
    print(f"dataset_id      : {report.get('dataset_id')}")
    print(f"dataset_type    : {report.get('dataset_type')}")
    print(f"evidence_level  : {report.get('evidence_level')}")
    print(f"样本数          : images={report['sample_count']['images']} "
          f"annotations={report['sample_count']['annotations']}")
    dist = report.get("category_distribution") or {}
    print(f"类别分布        : {dist if dist else '（无）'}")
    skipped = report.get("skipped_samples") or []
    print(f"跳过的坏样本    : {len(skipped)}")
    for s in skipped[:10]:
        print(f"  - [{s['type']}] id={s.get('id')}  {s.get('reason')}")
    if len(skipped) > 10:
        print(f"  ... 另 {len(skipped) - 10} 条")
    warnings = report.get("warnings") or []
    print(f"警告            : {len(warnings)}")
    for w in warnings[:5]:
        print(f"  - {w}")
    if report.get("isolation_violation"):
        print("✗ 跨 split 泄漏！")
        for v in report.get("leakage", []):
            print(f"  - {v}")
    if report.get("problems"):
        print("✗ 问题：")
        for p in report["problems"][:10]:
            print(f"  - {p}")
    if report.get("status") in ("valid", "ok"):
        print("✓ 校验通过 / 转换完成")
    else:
        print(f"✗ 状态：{report.get('status')}")


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        data, h = load_json(args.input)
    except ConfigError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 2
    report = validate_dataset(
        data, source_path=args.input, images_root=args.images_root,
        check_files=not args.no_check_files,
        check_checksums=not args.no_check_checksums,
        verify_dimensions=args.verify_dimensions,
    )
    _print_report(report, "Oceanus 数据集校验")
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入: {args.report}")
    if report["status"] == "invalid":
        return 1
    return 0


def _cmd_to_yolo(args: argparse.Namespace) -> int:
    try:
        data, _ = load_json(args.input)
    except ConfigError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 2
    report = seasight_to_yolo(
        data, args.images_root, args.output, strict=args.strict, source_path=args.input
    )
    _print_report(report, "Oceanus → YOLO 导出")
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入: {args.report}")
    if args.strict and report["skipped_samples"]:
        return 1
    return 0


def _cmd_from_yolo(args: argparse.Namespace) -> int:
    source = {}
    if args.source_device or args.source_region or args.source_note:
        source = {
            "device": args.source_device or "",
            "region": args.source_region or "",
            "date_range": None,
            "license": "",
            "acquisition_note": args.source_note or "",
        }
    try:
        report = yolo_to_seasight(
            args.images, args.labels,
            dataset_type=args.dataset_type,
            evidence_level=args.evidence_level,
            output=args.output,
            dataset_id=args.dataset_id,
            split=args.split,
            categories=[c.strip() for c in args.categories.split(",")] if args.categories else None,
            source=source,
            captured_at=args.captured_at,
            skip_image_hash=args.skip_image_hash,
            contract_reference=args.contract_reference,
        )
    except ConfigError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 1 if "没有" in str(e) or "全部被跳过" in str(e) else 2
    except DependencyError as e:
        print(f"[错误] {e}", file=sys.stderr)
        return 3
    _print_report(report, "YOLO → Oceanus 导入")
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入: {args.report}")
    print(f"输出      : {report['output']}")
    print(f"输出 sha256: {report['output_sha256']}")
    if args.strict and report["skipped_samples"]:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="convert_annotations",
        description="Oceanus COCO-like JSON 标注转换与校验（WP-13）",
    )
    sub = ap.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="校验 Oceanus JSON")
    p_validate.add_argument("--input", required=True)
    p_validate.add_argument("--images-root", default=None, help="数据集根目录（校验图片路径/校验和）")
    p_validate.add_argument("--no-check-files", action="store_true")
    p_validate.add_argument("--no-check-checksums", action="store_true")
    p_validate.add_argument("--verify-dimensions", action="store_true")
    p_validate.add_argument("--report", default=None, help="报告 JSON 输出路径")
    p_validate.set_defaults(func=_cmd_validate)

    p_toyolo = sub.add_parser("to-yolo", help="Oceanus JSON → YOLO txt")
    p_toyolo.add_argument("--input", required=True)
    p_toyolo.add_argument("--images-root", default=".", help="数据集根目录（用于定位 file_name）")
    p_toyolo.add_argument("--output", required=True, help="输出根目录（生成 labels/<split>/*.txt）")
    p_toyolo.add_argument("--report", default=None)
    p_toyolo.add_argument("--strict", action="store_true")
    p_toyolo.set_defaults(func=_cmd_to_yolo)

    p_fromyolo = sub.add_parser("from-yolo", help="YOLO txt → Oceanus JSON（导入）")
    p_fromyolo.add_argument("--images", required=True, help="图片目录")
    p_fromyolo.add_argument("--labels", required=True, help="标签目录（结构镜像 images/）")
    p_fromyolo.add_argument("--output", required=True, help="输出 dataset.json")
    p_fromyolo.add_argument("--dataset-type", required=True, choices=DATASET_TYPES,
                            help="planned|synthetic|quasi_real|real")
    p_fromyolo.add_argument("--evidence-level", default=None,
                            help="E0~E4；缺省按 dataset_type 最低档（real 默认 E3，须给来源）")
    p_fromyolo.add_argument("--dataset-id", default=None)
    p_fromyolo.add_argument("--split", default="train", choices=SPLITS)
    p_fromyolo.add_argument("--categories", default=None,
                            help="逗号分隔类别名（默认 foam,plastic,fishing_gear,other）")
    p_fromyolo.add_argument("--source-device", default="")
    p_fromyolo.add_argument("--source-region", default="")
    p_fromyolo.add_argument("--source-note", default="")
    p_fromyolo.add_argument("--captured-at", default=None)
    p_fromyolo.add_argument("--skip-image-hash", action="store_true")
    p_fromyolo.add_argument("--contract-reference", default=None,
                            help="E4 凭证编号（合同/订单/验收），不提供则拒绝 E4")
    p_fromyolo.add_argument("--report", default=None)
    p_fromyolo.add_argument("--strict", action="store_true")
    p_fromyolo.set_defaults(func=_cmd_from_yolo)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
