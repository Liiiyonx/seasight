"""check_dataset.py 空数据集守卫测试（WP-06）。

纯逻辑测试：用 tmp 目录构造空/非空数据集，验证：
  - 空数据集 → not_evaluated（退出码 1），不打印"数据集健康"
  - 空数据集 + --write-stats → 拒绝写回，yaml 原样保留
  - 非空数据集行为不回归
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ml" / "scripts"))

import check_dataset  # noqa: E402

_CONFIG_TEMPLATE = """\
path: dataset
train: images/train
val: images/val
names:
  0: foam
  1: plastic
  2: fishing_gear
  3: other
stats:
  train_images: 0
  val_images: 0
  background_images: 0
  instances:
    foam: 0
    plastic: 0
    fishing_gear: 0
    other: 0
"""


def _make_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "data.yaml"
    cfg.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    return cfg


def _make_nonempty_dataset(tmp_path: Path) -> Path:
    cfg = _make_config(tmp_path)
    img_dir = tmp_path / "dataset" / "images" / "train"
    lbl_dir = tmp_path / "dataset" / "labels" / "train"
    img_dir.mkdir(parents=True)
    lbl_dir.mkdir(parents=True)
    (img_dir / "a.jpg").write_bytes(b"not-a-real-image")
    (lbl_dir / "a.txt").write_text("0 0.5 0.5 0.1 0.1\n", encoding="utf-8")
    return cfg


def test_empty_dataset_is_not_evaluated(tmp_path, capsys) -> None:
    """空数据集必须输出 not_evaluated，且不打印"数据集健康"。"""
    cfg = _make_config(tmp_path)
    rc = check_dataset.main(["--data", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 1, "空数据集应视为阻断性问题（not_evaluated）"
    assert "not_evaluated" in out, "输出必须包含 not_evaluated 状态"
    assert "数据集健康" not in out, "空数据集不得宣称健康"
    assert "虚假精度" in out or "任何指标" in out


def test_empty_dataset_refuses_write_stats(tmp_path, capsys) -> None:
    """空数据集 + --write-stats：拒绝写回，yaml 原样保留（不冒充已采集统计）。"""
    cfg = _make_config(tmp_path)
    before = cfg.read_text(encoding="utf-8")
    rc = check_dataset.main(["--data", str(cfg), "--write-stats"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "拒绝写回" in out
    assert cfg.read_text(encoding="utf-8") == before


def test_empty_dataset_is_not_evaluated_with_strict(tmp_path, capsys) -> None:
    cfg = _make_config(tmp_path)
    rc = check_dataset.main(["--data", str(cfg), "--strict"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "not_evaluated" in out


def test_nonempty_dataset_still_reports_health(tmp_path, capsys) -> None:
    """非空数据集行为不回归：无阻断性问题，退出码 0。"""
    cfg = _make_nonempty_dataset(tmp_path)
    rc = check_dataset.main(["--data", str(cfg)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "无阻断性问题" in out
    assert "not_evaluated" not in out


def test_nonempty_dataset_strict_flag_still_works(tmp_path, capsys) -> None:
    """非空但有警告（空 val / 缺类别样本）时 --strict 仍返回 1。"""
    cfg = _make_nonempty_dataset(tmp_path)
    rc = check_dataset.main(["--data", str(cfg), "--strict"])
    capsys.readouterr()
    assert rc == 1


def test_missing_config_returns_2(tmp_path, capsys) -> None:
    rc = check_dataset.main(["--data", str(tmp_path / "nope.yaml")])
    err = capsys.readouterr().err
    assert rc == 2
    assert "找不到" in err
