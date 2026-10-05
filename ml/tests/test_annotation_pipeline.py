"""Oceanus 感知数据标注转换与完整性校验测试（WP-13）。

覆盖冻结接口：
  - Oceanus COCO-like JSON 结构（12 个顶层字段、schema_version、标注必填字段）
  - 图片路径 / 类别 / bbox / 面积 / 重复 ID / 跨 split 泄漏 / 校验和
  - 证据等级纪律：合成最高 E2；real 需真实来源+复核；E4 不自动授予
  - YOLO txt ↔ Oceanus JSON 双向转换
  - 报告字段：输入文件哈希、样本数、类别分布、跳过的坏样本、evidence_level

全部离线：图片由 Pillow 内存生成到临时目录，不访问公网、不调用模型。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ml" / "scripts"))

import convert_annotations as ca  # noqa: E402
import validate_dataset_integrity as vdi  # noqa: E402

from PIL import Image  # noqa: E402


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------

def make_png(path: Path, size=(100, 80), color=(120, 140, 160)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="PNG")


def write_label(lbl_dir: Path, name: str, content: str) -> None:
    lbl_dir.mkdir(parents=True, exist_ok=True)
    (lbl_dir / name).write_text(content, encoding="utf-8")


def valid_dataset(tmp_path: Path):
    """返回 (数据根目录, 合法 synthetic E1 数据集)。"""
    root = tmp_path / "data"
    names = ["a.png", "b.png"]
    for n in names:
        make_png(root / "images" / "train" / n)
    images, annotations = [], []
    for i, n in enumerate(names, start=1):
        images.append({
            "id": i, "file_name": f"images/train/{n}",
            "width": 100, "height": 80, "split": "train",
        })
        annotations.append({
            "id": i, "image_id": i, "category_id": 0,
            "bbox": [10.0, 20.0, 30.0, 40.0], "area": 1200.0, "iscrowd": 0,
            "source_type": "synthetic", "review_status": "reviewed",
            "reviewer": "pipeline",
        })
    # 第二张图加一个 plastic 实例，验证类别分布
    annotations.append({
        "id": 3, "image_id": 2, "category_id": 1,
        "bbox": [50.0, 10.0, 20.0, 20.0], "area": 400.0, "iscrowd": 0,
        "source_type": "synthetic", "review_status": "reviewed",
    })
    sha = {im["file_name"]: ca.sha256_file(root / im["file_name"]) for im in images}
    dataset = {
        "schema_version": "1.0",
        "dataset_id": "test_valid_001",
        "dataset_type": "synthetic",
        "evidence_level": "E1",
        "source": {"device": "synthetic-v1", "region": "lab", "acquisition_note": "合成帧"},
        "captured_at": "2026-09-19T10:00:00+08:00",
        "camera": {"model": "synthetic", "lens": "", "resolution": [100, 80]},
        "images": images,
        "annotations": annotations,
        "categories": [
            {"id": 0, "name": "foam", "supercategory": "marine_litter"},
            {"id": 1, "name": "plastic", "supercategory": "marine_litter"},
        ],
        "calibration": {"camera_matrix": None, "dist_coeffs": None, "homography": None,
                        "square_size_m": None, "pattern": None, "ref": ""},
        "checksums": {"algorithm": "sha256", "values": sha},
    }
    return root, dataset


def write_dataset(tmp_path: Path, dataset: dict) -> Path:
    p = tmp_path / "dataset.json"
    p.write_text(ca.json.dumps(dataset, ensure_ascii=False), encoding="utf-8")
    return p


# ----------------------------------------------------------------------
# 结构校验
# ----------------------------------------------------------------------

def test_required_top_level_fields_enforced(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    del ds["checksums"]
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "invalid"
    assert any("顶层字段 checksums" in p for p in rep["problems"])


def test_schema_version_enforced(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    ds["schema_version"] = "0.9"
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "invalid"
    assert any("schema_version" in p for p in rep["problems"])


def test_valid_dataset_passes(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "valid"
    assert rep["sample_count"] == {"images": 2, "annotations": 3}
    assert rep["category_distribution"] == {"foam": 2, "plastic": 1}
    assert rep["evidence_level"] == "E1"
    assert rep["isolation_violation"] is False
    assert rep["skipped_samples"] == []


def test_annotation_missing_required_field(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    del ds["annotations"][0]["review_status"]
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "invalid"
    kinds = {s["type"] for s in rep["skipped_samples"]}
    assert "annotation" in kinds
    assert any("review_status" in s["reason"] for s in rep["skipped_samples"])


def test_invalid_source_type_rejected(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    ds["annotations"][0]["source_type"] = "ai_generated"
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "invalid"
    assert any("source_type" in s["reason"] for s in rep["skipped_samples"])


def test_invalid_review_status_rejected(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    ds["annotations"][0]["review_status"] = "maybe"
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "invalid"


def test_bbox_and_area_validation(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    ds["annotations"][0]["bbox"] = [10.0, 20.0, -5.0, 40.0]  # w<=0
    ds["annotations"][1]["area"] = 999999.0                    # 与 bbox 不符
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "invalid"
    skipped = [s for s in rep["skipped_samples"] if s["type"] == "annotation"]
    assert any("宽高必须为正" in s["reason"] for s in skipped)
    assert any("不一致" in s["reason"] for s in skipped)


def test_bbox_out_of_image_bounds(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    ds["annotations"][0]["bbox"] = [90.0, 70.0, 30.0, 20.0]  # 超出 100x80
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "invalid"
    assert any("超出图片尺寸" in s["reason"] for s in rep["skipped_samples"])


def test_duplicate_image_id(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    ds["images"][1]["id"] = 1  # 与第一张重复
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "invalid"
    assert any(s["type"] == "image" and "重复" in s["reason"] for s in rep["skipped_samples"])


def test_duplicate_annotation_id(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    ds["annotations"][1]["id"] = 1
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "invalid"
    assert any("annotation id=1 重复" in s["reason"] for s in rep["skipped_samples"])


def test_missing_image_file(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    ds["images"][0]["file_name"] = "images/train/ghost.png"  # 磁盘不存在
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "invalid"
    assert any("不存在" in s["reason"] for s in rep["skipped_samples"])


def test_checksum_mismatch(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    ds["checksums"]["values"]["images/train/a.png"] = "0" * 64
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "invalid"
    assert any("校验和不一致" in s["reason"] for s in rep["skipped_samples"])


def test_cross_split_leakage_detected(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    leak = dict(ds["images"][0])
    leak["id"] = 99
    leak["split"] = "test"
    ds["images"].append(leak)
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["isolation_violation"] is True
    assert any("泄漏" in p for p in rep["problems"])


# ----------------------------------------------------------------------
# 证据等级纪律
# ----------------------------------------------------------------------

def test_synthetic_cannot_be_e3(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    ds["evidence_level"] = "E3"
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "invalid"
    assert any("E3" in p and "合成" in p for p in rep["problems"])


def test_synthetic_annotation_cannot_pollute_e3_dataset(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    ds["dataset_type"] = "real"
    ds["evidence_level"] = "E3"
    ds["source"]["acquisition_note"] = "2026-09-19 连江养殖区现场采集"
    ds["annotations"][0]["source_type"] = "synthetic"  # 合成标注混入真实数据集
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "invalid"
    assert any("合成" in s["reason"] and "E" in s["reason"] for s in rep["skipped_samples"])


def test_real_requires_acquisition_source(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    ds["dataset_type"] = "real"
    ds["evidence_level"] = "E3"
    ds["source"] = {"device": "", "region": "", "acquisition_note": ""}
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "invalid"
    assert any("真实采集来源" in p for p in rep["problems"])


def test_real_annotation_requires_review(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    ds["dataset_type"] = "real"
    ds["evidence_level"] = "E3"
    ds["source"]["acquisition_note"] = "2026-09-19 现场采集"
    for a in ds["annotations"]:
        a["source_type"] = "real"
        a["review_status"] = "unreviewed"
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "invalid"
    assert any("real 标注必须带复核状态" in s["reason"] for s in rep["skipped_samples"])


def test_e4_never_auto_granted(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    ds["dataset_type"] = "real"
    ds["evidence_level"] = "E4"  # 无 evidence_upgrade
    ds["source"]["acquisition_note"] = "现场采集"
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "invalid"
    assert any("E4" in p and "不自动授予" in p for p in rep["problems"])

    # 显式凭证 + 全部 real 标注（real 需复核）才接受
    ds["evidence_upgrade"] = {"basis": "contract", "reference": "CON-2026-001"}
    for a in ds["annotations"]:
        a["source_type"] = "real"
        a["review_status"] = "reviewed"
    rep2 = ca.validate_dataset(ds, images_root=root)
    assert rep2["status"] == "valid"


def test_planned_dataset_must_be_empty(tmp_path) -> None:
    root, ds = valid_dataset(tmp_path)
    ds["dataset_type"] = "planned"
    ds["evidence_level"] = "E0"
    rep = ca.validate_dataset(ds, images_root=root)
    assert rep["status"] == "invalid"
    assert any("planned" in p for p in rep["problems"])


def test_empty_planned_dataset_is_valid_but_not_evaluated(tmp_path) -> None:
    ds = {
        "schema_version": "1.0",
        "dataset_id": "empty_plan_001",
        "dataset_type": "planned",
        "evidence_level": "E0",
        "source": {"device": "", "region": "", "acquisition_note": "规划中，未采集"},
        "captured_at": "2026-09-19T10:00:00+08:00",
        "camera": {"model": "", "resolution": None},
        "images": [],
        "annotations": [],
        "categories": [{"id": 0, "name": "foam", "supercategory": "marine_litter"}],
        "calibration": {"camera_matrix": None, "dist_coeffs": None, "homography": None,
                        "square_size_m": None, "pattern": None, "ref": ""},
        "checksums": {"algorithm": "sha256", "values": {}},
    }
    rep = ca.validate_dataset(ds)
    assert rep["status"] == "valid"
    assert rep["evaluation_status"] == "not_evaluated"
    assert rep["sample_count"] == {"images": 0, "annotations": 0}


# ----------------------------------------------------------------------
# YOLO ↔ Oceanus 转换
# ----------------------------------------------------------------------

def test_yolo_to_seasight_import(tmp_path: Path) -> None:
    img_dir = tmp_path / "yolo" / "images"
    lbl_dir = tmp_path / "yolo" / "labels"
    make_png(img_dir / "a.png", (100, 80))
    make_png(img_dir / "b.png", (100, 80))
    write_label(lbl_dir, "a.txt", "0 0.5 0.5 0.2 0.1\n")
    write_label(lbl_dir, "b.txt", "1 0.25 0.25 0.1 0.1\n")

    out = tmp_path / "imported.json"
    report = ca.yolo_to_seasight(
        img_dir, lbl_dir,
        dataset_type="quasi_real", evidence_level="E2",
        output=out, dataset_id="import_test_001",
        source={"device": "quay-cam-01", "region": "dock", "acquisition_note": "码头受控拍摄"},
        captured_at="2026-09-19T10:00:00+08:00",
    )
    assert report["status"] == "ok"
    assert report["sample_count"] == {"images": 2, "annotations": 2}
    assert report["input_files"], "报告必须包含输入标签文件哈希"
    assert all(f["sha256"] for f in report["input_files"])

    ds = ca.load_json(out)[0]
    # 导入纪律：imported + unreviewed
    for ann in ds["annotations"]:
        assert ann["source_type"] == "imported"
        assert ann["review_status"] == "unreviewed"
    # bbox 换算：a 图 100x80，(0.5,0.5,0.2,0.1) → (40, 36, 20, 8)
    a_bbox = ds["annotations"][0]["bbox"]
    assert a_bbox == pytest.approx([40.0, 36.0, 20.0, 8.0], abs=0.01)
    assert ds["annotations"][0]["area"] == pytest.approx(160.0, rel=0.01)
    # checksums（key 与 images[].file_name 相对图片根目录一致）
    assert ds["checksums"]["algorithm"] == "sha256"
    assert "a.png" in ds["checksums"]["values"]
    # 校验导入结果合法
    rep = ca.validate_dataset(ds, images_root=img_dir)
    assert rep["status"] == "valid"


def test_yolo_to_seasight_bad_lines_skipped(tmp_path: Path) -> None:
    img_dir = tmp_path / "imgs"
    lbl_dir = tmp_path / "lbls"
    make_png(img_dir / "a.png")
    write_label(lbl_dir, "a.txt",
        "0 0.5 0.5 0.2 0.1\n"
        "0 0.5 0.5 0.2 0.1 9\n"      # 6 字段 → 坏行
        "5 0.5 0.5 0.2 0.1\n"        # 类别越界 → 坏行
        "0 1.5 0.5 0.2 0.1\n",       # 未归一化 → 坏行
    )
    out = tmp_path / "imported.json"
    report = ca.yolo_to_seasight(
        img_dir, lbl_dir, dataset_type="synthetic", evidence_level="E1",
        output=out, dataset_id="import_bad_001",
    )
    assert report["sample_count"]["images"] == 1
    assert report["sample_count"]["annotations"] == 1
    assert len(report["skipped_samples"]) == 3
    reasons = " ".join(s["reason"] for s in report["skipped_samples"])
    assert "字段数 6 ≠ 5" in reasons and "类别索引 5 越界" in reasons and "未归一化" in reasons


def test_yolo_to_seasight_rejects_real_without_source(tmp_path: Path) -> None:
    img_dir = tmp_path / "imgs"
    lbl_dir = tmp_path / "lbls"
    make_png(img_dir / "a.png")
    write_label(lbl_dir, "a.txt", "0 0.5 0.5 0.2 0.1\n")
    with pytest.raises(ca.ConfigError, match="real 数据集必须提供"):
        ca.yolo_to_seasight(
            img_dir, lbl_dir, dataset_type="real", evidence_level="E3",
            output=tmp_path / "x.json", dataset_id="x",
        )


def test_yolo_to_seasight_rejects_e4_without_contract(tmp_path: Path) -> None:
    img_dir = tmp_path / "imgs"
    lbl_dir = tmp_path / "lbls"
    make_png(img_dir / "a.png")
    write_label(lbl_dir, "a.txt", "0 0.5 0.5 0.2 0.1\n")
    with pytest.raises(ca.ConfigError, match="E4 需要 --contract-reference"):
        ca.yolo_to_seasight(
            img_dir, lbl_dir, dataset_type="real", evidence_level="E4",
            output=tmp_path / "x.json", dataset_id="x",
            source={"device": "cam", "region": "r", "acquisition_note": "n"},
        )


def test_seasight_to_yolo_export(tmp_path: Path) -> None:
    root, ds = valid_dataset(tmp_path)
    out = tmp_path / "yolo_out"
    report = ca.seasight_to_yolo(ds, root, out)
    assert report["export"]["exported_images"] == 2
    assert report["export"]["negative_images"] == 0

    lbl = out / "labels" / "train" / "a.txt"
    assert lbl.exists()
    line = lbl.read_text(encoding="utf-8").strip()
    parts = line.split()
    assert len(parts) == 5
    assert parts[0] == "0"
    # [10,20,30,40] @ 100x80 → cx=0.25 cy=0.5 w=0.3 h=0.5
    cx, cy, wn, hn = (float(v) for v in parts[1:])
    assert cx == pytest.approx(0.25, abs=1e-6)
    assert cy == pytest.approx(0.5, abs=1e-6)
    assert wn == pytest.approx(0.3, abs=1e-6)
    assert hn == pytest.approx(0.5, abs=1e-6)
    assert all(0.0 <= v <= 1.0 for v in (cx, cy, wn, hn))


def test_seasight_to_yolo_skips_bad_annotations(tmp_path: Path) -> None:
    root, ds = valid_dataset(tmp_path)
    ds["annotations"][0]["bbox"] = [10.0, 20.0, -5.0, 40.0]  # 坏样本
    out = tmp_path / "yolo_out"
    report = ca.seasight_to_yolo(ds, root, out)
    assert report["skipped_samples"], "坏样本应被跳过并列出"
    # 坏标注不导出，好标注仍导出
    lbl_b = out / "labels" / "train" / "b.txt"
    assert lbl_b.exists()
    assert not (out / "labels" / "train" / "a.txt").exists()


def test_yolo_roundtrip_preserves_annotations(tmp_path: Path) -> None:
    """Oceanus → YOLO → Oceanus：标注数量与类别分布保持一致。"""
    root, ds = valid_dataset(tmp_path)
    yolo_out = tmp_path / "yolo_out"
    ca.seasight_to_yolo(ds, root, yolo_out)

    imported_out = tmp_path / "reimported.json"
    report = ca.yolo_to_seasight(
        root / "images" / "train", yolo_out / "labels" / "train",
        dataset_type="synthetic", evidence_level="E1",
        output=imported_out, dataset_id="roundtrip_001",
    )
    assert report["sample_count"]["annotations"] == 3
    assert report["category_distribution"] == {"foam": 2, "plastic": 1}


# ----------------------------------------------------------------------
# 报告字段与 CLI
# ----------------------------------------------------------------------

def test_report_contains_required_sections(tmp_path: Path) -> None:
    root, ds = valid_dataset(tmp_path)
    jp = write_dataset(tmp_path, ds)
    rep = ca.validate_dataset(ds, source_path=jp, images_root=root)
    assert rep["input_files"], "报告必须包含输入文件哈希"
    assert rep["input_files"][0]["sha256"] == ca.sha256_file(jp)
    for key in ("sample_count", "category_distribution", "skipped_samples",
                "evidence_level", "dataset_type", "dataset_id", "status"):
        assert key in rep


def test_validate_cli_exit_codes(tmp_path: Path, capsys) -> None:
    root, ds = valid_dataset(tmp_path)
    good = tmp_path / "good.json"
    good.write_text(ca.json.dumps(ds), encoding="utf-8")

    assert ca.main(["validate", "--input", str(good), "--images-root", str(root)]) == 0

    bad = tmp_path / "bad.json"
    ds2 = dict(ds)
    ds2["evidence_level"] = "E3"  # synthetic 越级
    bad.write_text(ca.json.dumps(ds2), encoding="utf-8")
    assert ca.main(["validate", "--input", str(bad)]) == 1

    assert ca.main(["validate", "--input", str(tmp_path / "nope.json")]) == 2


def test_validate_dataset_integrity_cli(tmp_path: Path) -> None:
    root, ds = valid_dataset(tmp_path)
    good = tmp_path / "good.json"
    good.write_text(ca.json.dumps(ds), encoding="utf-8")
    report_json = tmp_path / "report.json"
    rc = vdi.main(["--input", str(good), "--images-root", str(root),
                   "--report", str(report_json)])
    assert rc == 0
    merged = ca.json.loads(report_json.read_text(encoding="utf-8"))
    assert merged["status"] == "valid"
    assert merged["inputs"][0]["sha256"] == ca.sha256_file(good)
    assert merged["reports"][0]["status"] == "valid"


def test_validate_dataset_integrity_cross_file_leakage(tmp_path: Path) -> None:
    root, ds = valid_dataset(tmp_path)
    train = tmp_path / "train.json"
    test = tmp_path / "test.json"
    ds["images"][0]["split"] = "train"
    ds2 = dict(ds)
    ds2["images"] = [dict(ds["images"][0])]
    ds2["images"][0]["split"] = "test"          # 同一文件进入 test
    ds2["dataset_id"] = "leak_test"
    train.write_text(ca.json.dumps(ds), encoding="utf-8")
    test.write_text(ca.json.dumps(ds2), encoding="utf-8")
    rc = vdi.main(["--input", str(train), "--input", str(test)])
    assert rc == 1
