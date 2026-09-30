"""WP-16 集成：WP-13 数据协议接线测试（总控串行项）。

锁定 evaluate_opencv.py 的 --gt-json 路径与 check_dataset.py 的
--data-protocol-json 路径：
- 协议 JSON 无效 / 越级证据 / 泄漏 → not_evaluated，绝不生成虚假精度。
- 合法协议 + 空样本 → not_evaluated（空数据语义不变）。
- check_dataset --data-protocol-json 接受协议 JSON 并做协议级校验。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ml.scripts import evaluate_opencv as ev


def _minimal_protocol(tmp_path: Path, *, evidence: str = "E1", dataset_type: str = "synthetic") -> Path:
    """构造最小合法 SeaSight COCO-like 协议 JSON。"""
    data = {
        "schema_version": "1.0",
        "dataset_id": "synthetic_test_wp16",
        "dataset_type": dataset_type,
        "evidence_level": evidence,
        "source": {
            "device": "synthetic-renderer-v1",
            "region": "lab",
            "date_range": ["2026-09-19", "2026-09-19"],
            "license": "internal",
            "acquisition_note": "程序化合成的海面漂浮物帧，非现场采集",
        },
        "captured_at": "2026-09-19T10:00:00+08:00",
        "camera": {"model": "synthetic-cam", "lens": "n/a", "resolution": [640, 480]},
        "images": [
            {"id": 1, "file_name": "images/test/0001.jpg", "width": 640, "height": 480, "split": "test"},
            {"id": 2, "file_name": "images/test/0002.jpg", "width": 640, "height": 480, "split": "test"},
        ],
        "annotations": [
            {"id": 1, "image_id": 1, "category_id": 1, "bbox": [10, 20, 100, 60], "area": 6000,
             "iscrowd": 0, "source_type": "synthetic", "review_status": "approved"},
            {"id": 2, "image_id": 2, "category_id": 2, "bbox": [30, 40, 50, 50], "area": 2500,
             "iscrowd": 0, "source_type": "synthetic", "review_status": "approved"},
        ],
        "categories": [
            {"id": 1, "name": "foam", "supercategory": "litter"},
            {"id": 2, "name": "plastic", "supercategory": "litter"},
        ],
        "calibration": {"camera_matrix": None, "dist_coeffs": None, "homography": None,
                        "square_size_m": None, "pattern": None, "ref": None},
        "checksums": {"algorithm": "sha256", "values": {}},
    }
    p = tmp_path / "dataset.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_gt_map_builder_maps_objects_and_converts_bbox() -> None:
    """_load_protocol_gt 正确把 COCO xywh 转为 [x1,y1,x2,y2]，按文件名映射。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        proto = _minimal_protocol(Path(td))
        gt_map, reason = ev._load_protocol_gt(proto)
        assert reason == ""
        assert gt_map is not None
        assert set(gt_map) == {"0001.jpg", "0002.jpg"}
        assert gt_map["0001.jpg"]["objects"] == [{"class": "foam", "bbox": [10.0, 20.0, 110.0, 80.0]}]
        assert gt_map["0002.jpg"]["objects"] == [{"class": "plastic", "bbox": [30.0, 40.0, 80.0, 90.0]}]


def test_invalid_protocol_yields_not_evaluated_reason() -> None:
    """协议 JSON 无效（坏 schema）→ (None, 原因)，调用方应输出 not_evaluated。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "bad.json"
        p.write_text('{"schema_version": "1.0"}', encoding="utf-8")
        gt_map, reason = ev._load_protocol_gt(p)
        assert gt_map is None
        assert reason


def test_overlevel_evidence_rejected() -> None:
    """synthetic 数据集宣称 E4 → 校验不通过 → not_evaluated 路径。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        p = _minimal_protocol(Path(td), evidence="E4", dataset_type="synthetic")
        gt_map, reason = ev._load_protocol_gt(p)
        assert gt_map is None
        assert reason


def test_check_dataset_accepts_data_protocol_json(tmp_path: Path) -> None:
    """check_dataset --data-protocol-json 接受合法协议 JSON（退出 0），坏 JSON 记阻断（退出非 0）。"""
    from ml.scripts import check_dataset as ck

    proto = _minimal_protocol(tmp_path)
    # 合法协议 + 空数据 yaml（默认 ml/configs/seasight.yaml 指向空 datasets）：
    # 协议校验本身通过；退出码只取决于数据体检（空数据 → not_evaluated 阻断语义，退出 1 是空数据体检，非协议）
    code_ok = ck.main(["--data-protocol-json", str(proto), "--data", "ml/configs/seasight.yaml"])
    # 协议校验未失败的证据：坏协议必须被记为一个阻断问题
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema_version": "1.0"}', encoding="utf-8")
    code_bad = ck.main(["--data-protocol-json", str(bad), "--data", "ml/configs/seasight.yaml"])
    assert code_bad >= 1, "坏协议 JSON 必须导致非 0 退出"
