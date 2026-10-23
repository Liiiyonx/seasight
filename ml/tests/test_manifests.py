"""数据集清单机制测试（WP-06）—— 四类分集隔离纪律。

验证 ml/datasets/manifests/ 下的模板与四类示例清单满足冻结口径：
  - 四类分集齐全：train / val / test / blind
  - 每个清单必须写明物理或逻辑隔离方式（不允许留空）
  - 独立测试集 / 现场盲测集禁止出现在 train|val 的同一文件集合（隔离纪律声明）
  - evidence_level 不越界（合成帧最高 E2）
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ml" / "scripts"))

import evaluate_opencv as ev  # noqa: E402

MANIFESTS = ROOT / "ml" / "datasets" / "manifests"

EXPECTED_TYPES = {"train", "val", "test", "blind"}


def _examples() -> dict[str, dict]:
    return ev.discover_manifests(MANIFESTS)


def test_four_split_types_exist() -> None:
    assert set(_examples()) == EXPECTED_TYPES


@pytest.mark.parametrize("mtype", sorted(EXPECTED_TYPES))
def test_each_manifest_documents_isolation(mtype: str) -> None:
    m = _examples()[mtype]
    iso = m["isolation"]
    assert iso["method"] in ("physical", "logical"), f"{mtype}: 必须写明 physical/logical 隔离"
    note = str(iso.get("physical_note") or "")
    rule = str(iso.get("logical_rule") or "")
    assert note.strip() or rule.strip(), f"{mtype}: isolation 说明不允许留空"


@pytest.mark.parametrize("mtype", sorted(EXPECTED_TYPES))
def test_each_manifest_has_valid_evidence(mtype: str) -> None:
    m = _examples()[mtype]
    if m["status"] == "planned":
        # 未就绪清单一律 E0（没有数据就没有证据），即使声明了合成帧
        assert m["evidence_level"] == "E0"
    else:
        assert m["evidence_level"] in ev.ALLOWED_EVIDENCE[m["data_type"]], (
            f"{mtype}: evidence_level {m['evidence_level']} 超出 {m['data_type']} 上限"
        )


def test_template_exists_and_is_not_a_real_manifest() -> None:
    template = MANIFESTS / "template_manifest.yaml"
    assert template.exists()
    # 模板不得被清单发现逻辑当成真实分集
    discovered = ev.discover_manifests(MANIFESTS)
    assert "template" not in discovered


def test_test_and_blind_declare_no_overlap_with_train() -> None:
    """独立测试集与盲测集必须在清单里声明与 train/val 的隔离规则。"""
    for mtype in ("test", "blind"):
        m = _examples()[mtype]
        text = (str(m["isolation"].get("physical_note", "")) + " " +
                str(m["isolation"].get("logical_rule", "")))
        assert ("不重叠" in text or "互不" in text or "不得" in text or
                "不同" in text or "隔离" in text), f"{mtype}: 清单未声明与训练分集的隔离"


def test_duplicate_manifest_type_is_rejected(tmp_path: Path) -> None:
    """同一目录出现两个同类型清单 → 拒绝（防清单漂移）。"""
    body = (
        'schema_version: "1.0"\nmanifest_type: train\nname: a\nstatus: ready\n'
        "isolation:\n  method: physical\n  physical_note: x\n"
        "data_type: synthetic\nevidence_level: E1\nlocation:\n  images: i\n"
    )
    (tmp_path / "a.yaml").write_text(body, encoding="utf-8")
    (tmp_path / "b.yaml").write_text(body, encoding="utf-8")
    with pytest.raises(ev.ConfigError):
        ev.discover_manifests(tmp_path)


def test_schema_version_is_pinned() -> None:
    for mtype, m in _examples().items():
        assert m["schema_version"] == "1.0", f"{mtype}: schema_version 必须锁定 1.0"
