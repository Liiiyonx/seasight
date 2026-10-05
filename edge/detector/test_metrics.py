"""指标口径测试 —— 76% 抑制率只针对时序链路；precision/recall 零分母 = not_evaluated。

运行（仓库根）：
    python -m pytest edge/detector/test_metrics.py -q

冻结口径（WP-06 / 计划书 §6.1、§7.1）：
  1. 「76% 抑制率」= (fed - absorbed) / fed，分母是**检测数**，只针对**时序链路**
     （edge/simulator 的 TemporalValidator），不是检测器精度，禁止外推。
  2. precision/recall 任一分母为零 → 该指标为 None + not_evaluated，
     两个分母都为零 → 整项 not_evaluated，绝不生成虚假精度。
  3. 合成帧证据最高 E1-E2，禁止写成 E3/E4。

纯逻辑测试，不需要真实数据集；cv2 只用于端到端接线验证（缺失时跳过）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
EDGE = HERE.parent
ROOT = EDGE.parent

sys.path.insert(0, str(EDGE / "simulator"))
sys.path.insert(0, str(ROOT / "ml" / "scripts"))

from simulator import TemporalValidator  # noqa: E402
from evaluate_opencv import (  # noqa: E402
    compute_class_metrics,
    resolve_evidence,
    suppression_rate,
)


# ----------------------------------------------------------------------
# 76% 抑制率口径：只针对时序链路
# ----------------------------------------------------------------------

class TestSuppressionRateTemporalScope:
    def test_formula_bounds(self) -> None:
        """抑制率恒在 0~1；fed<=0 时必须为 None（不得编造）。"""
        assert suppression_rate(599, 144) == pytest.approx(0.7596, abs=1e-4)  # ≈76%
        assert suppression_rate(100, 100) == 0.0
        assert suppression_rate(100, 0) == 1.0
        assert 0.0 <= suppression_rate(50, 12) <= 1.0
        # 极端输入也夹在 [0,1]
        assert 0.0 <= suppression_rate(50, 99) <= 1.0   # absorbed>fed → 抑制率 0
        # 无输入 → None，不是 0（0 会被误读成"零抑制"，实际是"没数据"）
        assert suppression_rate(0, 0) is None
        assert suppression_rate(-1, 0) is None

    def test_readme_76_percent_claim_derivation(self) -> None:
        """复现 README 的 76% 口径：200 帧 / 599 原始检测 / 3 确认事件。

        口径推导：suppressed = fed - absorbed = 599 * 0.76 ≈ 455，
                   absorbed ≈ 144（被稳定跟踪的有效命中）。
        关键断言：抑制率**不能**用确认事件数作分母（3/599 ≈ 0.5%），
                  也不能把 76% 写成检测器精度 —— 它只是时序链路统计。
        """
        fed = 599
        claimed = 0.76
        absorbed = fed * (1.0 - claimed)
        assert absorbed == pytest.approx(143.76, abs=0.5)

        rate = suppression_rate(fed, int(round(absorbed)))
        assert rate == pytest.approx(claimed, abs=0.01)

        # 若误用 confirmed=3 作分母，结论会完全不同 —— 证明口径必须是检测数
        confirmed = 3
        wrong_ratio = confirmed / fed
        assert wrong_ratio == pytest.approx(0.005, abs=0.001)
        assert wrong_ratio != pytest.approx(claimed, abs=0.5)

        # 恒等式：fed = absorbed + suppressed（三个计数同单位，口径自洽）
        suppressed = fed - int(round(absorbed))
        assert suppressed + int(round(absorbed)) == fed

    def test_temporal_validator_stats_identity(self) -> None:
        """TemporalValidator 的 stats 必须满足 fed = absorbed + suppressed。

        用文档口径参数实例化（与 edge/config.yaml temporal 段一致），
        证明抑制率是从检测计数推导、只存在于时序链路内部的量。
        """
        v = TemporalValidator(
            window_frames=15, min_hits=3, grid_size=64, min_confidence=0.45,
            match_distance=60.0, max_misses=5, min_iou=0.25, decimation=4,
        )

        def det(cls: str, x: int, y: int, conf: float = 0.7) -> dict:
            return {"class": cls, "confidence": conf, "bbox": [x, y, x + 30, y + 30]}

        # 一个持续目标（固定位置，逐帧命中 → absorbed）+ 每帧随机噪声（→ suppressed）
        for i in range(40):
            v.push([det("foam", 320, 180, 0.8)])
            v.sweep(4)
        for i in range(10):
            v.push([det("foam", 100 + i * 37, 200 + i * 51, 0.5)])
            v.sweep(4)

        st = v.stats
        assert st["fed"] == st["absorbed"] + st["suppressed"], "计数口径必须自洽"
        assert st["fed"] > 0
        rate = suppression_rate(st["fed"], st["absorbed"])
        assert rate is not None
        assert 0.0 <= rate <= 1.0
        # 持续目标至少被确认 1 个 —— 它走的是 absorbed（有效命中）通道
        assert st["confirmed"] >= 1
        # 抑制率只统计"未被稳定跟踪的检测"，确认事件数不是分母
        assert rate != pytest.approx(st["confirmed"] / st["fed"], abs=0.05)

    def test_single_frame_noise_never_counts_as_absorbed(self) -> None:
        """单帧噪声只能进 suppressed，不能进 absorbed —— 这是 76% 的机制来源。"""
        v = TemporalValidator(decimation=1)
        v.push([{"class": "foam", "confidence": 0.9, "bbox": [10, 10, 40, 40]}])
        st = v.stats
        assert st["fed"] == 1
        assert st["absorbed"] == 0
        assert st["suppressed"] == 1
        assert suppression_rate(st["fed"], st["absorbed"]) == 1.0

    def test_low_confidence_is_part_of_temporal_suppression(self) -> None:
        """置信度门限是第一道闸：低分检测计入 suppressed（同单位检测数）。"""
        v = TemporalValidator(decimation=1, min_confidence=0.45)
        v.push([{"class": "foam", "confidence": 0.3, "bbox": [10, 10, 40, 40]}])
        st = v.stats
        assert st["fed"] == 1
        assert st["absorbed"] == 0
        assert st["suppressed"] == 1


# ----------------------------------------------------------------------
# precision / recall 零分母 → not_evaluated
# ----------------------------------------------------------------------

class TestZeroDenominatorSemantics:
    def test_no_gt_no_pred_is_not_evaluated(self) -> None:
        """无预测也无真值：整项 not_evaluated，数值一律 None，绝不写 0 冒充精度。"""
        m = compute_class_metrics(tp=0, fp=0, fn=0)
        assert m["status"] == "not_evaluated"
        assert m["precision"] is None
        assert m["recall"] is None
        assert m["f1"] is None

    def test_partial_zero_denominator(self) -> None:
        """有预测无真值：precision 可算（fp 全错→0.0），recall 分母为零→None。"""
        m = compute_class_metrics(tp=0, fp=5, fn=0)
        assert m["precision"] == 0.0
        assert m["recall"] is None
        assert m["f1"] is None
        assert m["status"] == "not_evaluated"

    def test_no_pred_with_ground_truth(self) -> None:
        """无预测有真值：recall=0.0 可算（漏检全部），precision 分母为零→None。"""
        m = compute_class_metrics(tp=0, fp=0, fn=2)
        assert m["recall"] == 0.0
        assert m["precision"] is None
        assert m["f1"] is None
        assert m["status"] == "not_evaluated"

    def test_normal_case(self) -> None:
        m = compute_class_metrics(tp=3, fp=1, fn=2)
        assert m["status"] == "evaluated"
        assert m["precision"] == pytest.approx(3 / 4)
        assert m["recall"] == pytest.approx(3 / 5)
        assert m["f1"] == pytest.approx(2 * (3 / 4) * (3 / 5) / ((3 / 4) + (3 / 5)))

    def test_tp_zero_with_both_denominators(self) -> None:
        """tp=0 但两个分母都非零：0.0 是合法数值（全错/全漏），不是 not_evaluated。"""
        m = compute_class_metrics(tp=0, fp=3, fn=1)
        assert m["status"] == "evaluated"
        assert m["precision"] == 0.0
        assert m["recall"] == 0.0
        assert m["f1"] == 0.0


# ----------------------------------------------------------------------
# evidence_level 上限（合成帧最高 E2，不得写成 E3/E4）
# ----------------------------------------------------------------------

class TestEvidenceLevelCap:
    def test_synthetic_cannot_claim_e3_or_e4(self) -> None:
        with pytest.raises(Exception):
            resolve_evidence("synthetic", "ready", "E3")
        with pytest.raises(Exception):
            resolve_evidence("synthetic", "ready", "E4")
        assert resolve_evidence("synthetic", "ready", "E2") == "E2"
        assert resolve_evidence("synthetic", "ready", None) == "E1"

    def test_planned_forces_e0(self) -> None:
        assert resolve_evidence("synthetic", "planned", "E2") == "E0"

    def test_real_defaults_to_e3_not_e4(self) -> None:
        assert resolve_evidence("real", "ready", None) == "E3"
        with pytest.raises(Exception):
            resolve_evidence("real", "ready", "E4")  # E4 需合同凭证，不自动授予


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
