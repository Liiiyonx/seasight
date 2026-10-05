"""evaluate_opencv.py 评测逻辑测试（WP-06）。

纯逻辑部分不依赖真实数据集，也不依赖 cv2（metrics/manifest/evidence/report 结构）；
端到端合成数据评测用内存生成帧验证（cv2 缺失时自动跳过）。

冻结口径断言：
  - 空数据集 / 清单未就绪 → not_evaluated，绝不产出数值
  - 每项指标带 evidence_level；合成帧最高 E2
  - 输出 JSON 含命令、日期、代码版本、样本量、分母、类别指标、confounder 分层
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ml" / "scripts"))

import evaluate_opencv as ev  # noqa: E402

REPO_CONFIG = ROOT / "ml" / "configs" / "seasight.yaml"
NAMES = ["foam", "plastic", "fishing_gear", "other"]


# ----------------------------------------------------------------------
# 指标函数
# ----------------------------------------------------------------------

class TestMetricFunctions:
    def test_suppression_rate_edges(self) -> None:
        assert ev.suppression_rate(0, 0) is None
        assert ev.suppression_rate(599, 144) == pytest.approx(0.7596, abs=1e-3)
        assert 0.0 <= ev.suppression_rate(10, 0) <= 1.0

    def test_zero_denominator_never_fabricates(self) -> None:
        m = ev.compute_class_metrics(0, 0, 0)
        assert m["status"] == "not_evaluated"
        assert m["precision"] is None and m["recall"] is None and m["f1"] is None
        m2 = ev.compute_class_metrics(2, 1, 0)
        assert m2["status"] == "evaluated"
        assert m2["precision"] == pytest.approx(2 / 3)
        assert m2["recall"] == 1.0

    def test_iou_matching(self) -> None:
        gt = [[10, 10, 60, 60]]
        det = [[12, 12, 62, 62]]  # 高度重叠
        assert ev.match_detections(gt, det, 0.5) == (1, 0, 0)
        det2 = [[500, 500, 560, 560]]  # 完全不重叠
        assert ev.match_detections(gt, det2, 0.5) == (0, 1, 1)


# ----------------------------------------------------------------------
# evidence 上限
# ----------------------------------------------------------------------

class TestEvidenceCaps:
    def test_synthetic_max_e2(self) -> None:
        assert ev.resolve_evidence("synthetic", "ready", "E2") == "E2"
        with pytest.raises(ev.ConfigError):
            ev.resolve_evidence("synthetic", "ready", "E3")
        with pytest.raises(ev.ConfigError):
            ev.resolve_evidence("synthetic", "ready", "E4")

    def test_planned_forces_e0(self) -> None:
        assert ev.resolve_evidence("synthetic", "planned", None) == "E0"

    def test_real_defaults_e3(self) -> None:
        assert ev.resolve_evidence("real", "ready", None) == "E3"
        with pytest.raises(ev.ConfigError):
            ev.resolve_evidence("real", "ready", "E4")


# ----------------------------------------------------------------------
# 清单校验与发现
# ----------------------------------------------------------------------

class TestManifestValidation:
    def test_valid_example_passes(self) -> None:
        data = ev._load_yaml(ROOT / "ml" / "datasets" / "manifests" / "example_test_independent.yaml")
        m = ev.validate_manifest(data, "example_test_independent.yaml")
        assert m["manifest_type"] == "test"
        assert m["evidence_level"] in ("E0", "E1", "E2")

    def test_missing_location_rejected(self, tmp_path) -> None:
        m = {
            "schema_version": "1.0", "manifest_type": "test", "name": "x",
            "status": "ready", "data_type": "synthetic", "evidence_level": "E1",
            "isolation": {"method": "physical", "physical_note": "独立目录"},
            "location": {},
        }
        with pytest.raises(ev.ConfigError):
            ev.validate_manifest(m, "bad.yaml")

    def test_synthetic_e3_rejected(self, tmp_path) -> None:
        m = {
            "schema_version": "1.0", "manifest_type": "test", "name": "x",
            "status": "ready", "data_type": "synthetic", "evidence_level": "E3",
            "isolation": {"method": "physical", "physical_note": "独立目录"},
            "location": {"images": "whatever"},
        }
        with pytest.raises(ev.ConfigError):
            ev.validate_manifest(m, "bad.yaml")

    def test_empty_isolation_rejected(self, tmp_path) -> None:
        m = {
            "schema_version": "1.0", "manifest_type": "test", "name": "x",
            "status": "ready", "data_type": "synthetic", "evidence_level": "E1",
            "isolation": {"method": "physical", "physical_note": "", "logical_rule": ""},
            "location": {"images": "whatever"},
        }
        with pytest.raises(ev.ConfigError):
            ev.validate_manifest(m, "bad.yaml")

    def test_discover_skips_template_and_finds_four_types(self) -> None:
        manifests = ev.discover_manifests(ROOT / "ml" / "datasets" / "manifests")
        assert set(manifests) == {"train", "val", "test", "blind"}

    def test_duplicate_type_rejected(self, tmp_path) -> None:
        body = (
            'schema_version: "1.0"\nmanifest_type: test\nname: a\nstatus: ready\n'
            "isolation:\n  method: physical\n  physical_note: x\n"
            "data_type: synthetic\nlocation:\n  images: i\n"
        )
        (tmp_path / "a.yaml").write_text(body, encoding="utf-8")
        (tmp_path / "b.yaml").write_text(body, encoding="utf-8")
        with pytest.raises(ev.ConfigError):
            ev.discover_manifests(tmp_path)


# ----------------------------------------------------------------------
# 报告结构与 not_evaluated 语义
# ----------------------------------------------------------------------

class TestReport:
    def test_no_manifests_is_not_evaluated(self, tmp_path) -> None:
        cfg = {"path": ".", "names": NAMES, "evaluation": {}}
        report = ev.build_report(
            cfg, REPO_CONFIG, tmp_path / "no_manifests", "test",
            0.5, 0, "python ml/scripts/evaluate_opencv.py", {}, {},
        )
        assert report["status"] == "not_evaluated"
        assert report["not_evaluated_reasons"]
        assert report["dataset"]["empty"] is True
        # 代码版本必须注明来源（仓库有 .git 用 git；无则 no_git_repo）+ 指纹
        assert report["code_version"]["source"] in ("git", "no_git_repo")
        assert report["code_version"]["value"]
        assert report["code_version"]["fingerprint"]
        # 报告必须含命令、日期、依赖、类别
        assert report["command"].startswith("python")
        assert report["date"]
        assert report["dependencies"] == {}
        assert report["config"]["names"] == NAMES

    def test_planned_manifest_is_not_evaluated(self, tmp_path) -> None:
        manifest_dir = tmp_path / "manifests"
        manifest_dir.mkdir()
        (manifest_dir / "test.yaml").write_text(
            (
                'schema_version: "1.0"\nmanifest_type: test\nname: planned_test\n'
                "status: planned\n"
                "isolation:\n  method: physical\n  physical_note: 独立目录\n"
                "data_type: synthetic\nevidence_level: E1\n"
                "location:\n  images: does_not_exist\n"
            ),
            encoding="utf-8",
        )
        manifests = ev.discover_manifests(manifest_dir)
        cfg = {"path": ".", "names": NAMES, "evaluation": {}}
        report = ev.build_report(cfg, REPO_CONFIG, manifest_dir, "test", 0.5, 0, "cmd", {}, manifests)
        assert report["status"] == "not_evaluated"
        assert any("planned" in r for r in report["not_evaluated_reasons"])
        assert report["dataset"]["evidence_level"] == "E0"
        assert report["metrics"]["per_class"] == {}

    def test_missing_split_is_not_evaluated(self, tmp_path) -> None:
        manifest_dir = tmp_path / "manifests"
        manifest_dir.mkdir()
        (manifest_dir / "train.yaml").write_text(
            (
                'schema_version: "1.0"\nmanifest_type: train\nname: t\nstatus: ready\n'
                "isolation:\n  method: logical\n  logical_rule: 按天切分\n"
                "data_type: synthetic\nevidence_level: E1\n"
                "location:\n  images: x\n"
            ),
            encoding="utf-8",
        )
        manifests = ev.discover_manifests(manifest_dir)
        cfg = {"path": ".", "names": NAMES, "evaluation": {}}
        report = ev.build_report(cfg, REPO_CONFIG, manifest_dir, "test", 0.5, 0, "cmd", {}, manifests)
        assert report["status"] == "not_evaluated"
        assert any("缺少 test" in r for r in report["not_evaluated_reasons"])


# ----------------------------------------------------------------------
# 端到端：合成数据 → evaluated（cv2 缺失时跳过）
# ----------------------------------------------------------------------

def _sea(seed: int, size: tuple[int, int] = (360, 640)):
    import numpy as np

    rng = np.random.default_rng(seed)
    base = np.zeros((size[0], size[1], 3), np.uint8)
    base[:] = (110, 100, 82)
    noise = rng.normal(0, 4, base.shape)
    return np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def _with_foam(seed: int):
    import cv2

    frame = _sea(seed)
    cv2.circle(frame, (320, 180), 24, (232, 235, 238), -1)  # V≈238 白团
    return frame


def test_build_report_evaluated_with_synthetic_data(tmp_path) -> None:
    pytest.importorskip("cv2")
    import cv2

    manifest_dir = tmp_path / "manifests"
    manifest_dir.mkdir()

    samples: list[dict] = []
    # 6 帧负样本（纯海面，无真值）+ 10 帧泡沫正样本（带真值与条件元数据）
    for i in range(6):
        p = tmp_path / f"neg_{i:03d}.jpg"
        cv2.imwrite(str(p), _sea(100 + i))
        samples.append({"path": str(p), "conditions": {"lighting": "daylight"}, "objects": []})
    for i in range(10):
        p = tmp_path / f"foam_{i:03d}.jpg"
        cv2.imwrite(str(p), _with_foam(200 + i))
        samples.append(
            {
                "path": str(p),
                "conditions": {"lighting": "daylight"},
                "objects": [{"class": "foam", "bbox": [290, 150, 350, 210]}],
            }
        )

    (manifest_dir / "test.yaml").write_text(
        (
            'schema_version: "1.0"\nmanifest_type: test\nname: synthetic_e2e\n'
            "status: ready\n"
            "isolation:\n  method: physical\n  physical_note: tmp 独立目录\n"
            "data_type: synthetic\nevidence_level: E1\n"
            "location:\n  images: null\n  video: null\n"
            "confounders:\n  - lighting\n"
            "samples:\n"
        )
        + "".join(
            f'  - path: {s["path"]}\n'
            f'    conditions: {{lighting: {s["conditions"]["lighting"]}}}\n'
            + (
                "    objects:\n"
                + "".join(
                    f"      - class: {o['class']}\n        bbox: {o['bbox']}\n"
                    for o in s["objects"]
                )
                if s["objects"]
                else "    objects: []\n"
            )
            for s in samples
        ),
        encoding="utf-8",
    )

    manifests = ev.discover_manifests(manifest_dir)
    assert "test" in manifests
    cfg = {"path": ".", "names": NAMES, "evaluation": {"confounders": ["lighting"], "temporal": {}}}
    report = ev.build_report(
        cfg, REPO_CONFIG, manifest_dir, "test", 0.5, 0, "cmd", {}, manifests
    )
    assert report["status"] == "evaluated", report["not_evaluated_reasons"]
    assert report["dataset"]["sample_size"] == 16
    assert report["dataset"]["evidence_level"] == "E1"
    # 分母：10 个泡沫真值框
    assert report["denominators"]["foam"]["gt_boxes"] == 10
    foam = report["metrics"]["per_class"]["foam"]
    assert foam["status"] == "evaluated"
    assert foam["precision"] is not None and 0.0 <= foam["precision"] <= 1.0
    assert foam["recall"] is not None and foam["recall"] >= 0.5, "合成泡沫应大多被检出"
    assert foam["evidence_level"] == "E1"
    # confounder 分层
    assert "lighting" in report["confounders"]
    assert "daylight" in report["confounders"]["lighting"]
    # 时序链路：无视频 → not_evaluated，且明确口径只针对时序链路
    tp = report["metrics"]["temporal_pipeline"]
    assert tp["status"] == "not_evaluated"
    assert tp["scope"] == "temporal_link_only"


def test_report_json_serializable(tmp_path) -> None:
    """报告必须可 JSON 序列化（无 NaN/Infinity/自定义对象）。"""
    cfg = {"path": ".", "names": NAMES, "evaluation": {}}
    report = ev.build_report(cfg, REPO_CONFIG, tmp_path / "x", "test", 0.5, 0, "cmd", {}, {})
    import json

    text = json.dumps(report, ensure_ascii=False, indent=2)
    assert '"status": "not_evaluated"' in text
