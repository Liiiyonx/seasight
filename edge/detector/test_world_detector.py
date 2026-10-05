"""开放词汇检测器（WorldDetector）测试。

运行：
    cd edge/detector && python -m pytest test_world_detector.py -v
（也已加入 Makefile 的 test-edge 目标）

★ 本文件不需要 torch / ultralytics
-----------------------------------
模型的加载路径靠**注入 runner** 绕开：契约、阈值、映射、ROI、降级
这些逻辑都不依赖真实权重，用假 runner 就能完整验证。
真实权重只在"跑一遍对照评测"时才需要（见 artifacts/metrics/）。

★ 这里有一条测试专门守着一个真实的坑
-------------------------------------
``test_world_output_is_dropped_by_cv_threshold_but_kept_by_world_threshold``：
原方案打算让开放词汇通道沿用 cv 通道的 0.45 时序门限。实测数据表明
该通道历史最高置信度为 0.184（见 world_detector 模块头部的标定表），
套用 0.45 会让它**一条检测都过不去**，而现象只是"world 通道没结果"。
所以本文件把它钉成两条断言：0.45 下必空、world 门限下必通。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2", reason="需要 opencv-python-headless")

HERE = Path(__file__).resolve().parent
EDGE = HERE.parent
ROOT = EDGE.parent

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(EDGE / "simulator"))

import label_map  # noqa: E402
from detector import CLASS_NAMES, CvDetector  # noqa: E402
from label_map import (  # noqa: E402
    CALIBRATION_PROMPTS,
    CONTRACT_CLASSES,
    DEMO_DEFAULT_CLASSES,
    DEMO_LIVE_PROMPTS,
    MATCH_EXACT,
    MATCH_FALLBACK,
    PROMPT_TO_CLASS,
    normalize,
    unmapped_prompts,
)
from simulator import TemporalValidator  # noqa: E402
from world_detector import (  # noqa: E402
    BACKEND_NAME,
    DEFAULT_WORLD_CONFIG,
    PROJECT_ROOT,
    WorldDetector,
    resolve_weights_path,
)

# cv 通道的时序门限（0.45）。本文件里凡是出现它，都是为了证明"不能套用到 world 通道"。
CV_TEMPORAL_MIN_CONFIDENCE = 0.45


# ----------------------------------------------------------------------
# 合成帧与假 runner
# ----------------------------------------------------------------------
def _sea(seed: int = 0, size: tuple[int, int] = (360, 640)) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = np.zeros((size[0], size[1], 3), np.uint8)
    base[:] = (110, 100, 82)
    noise = rng.normal(0, 4, base.shape)
    return np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def _fake_runner(raw_by_call: list[list[dict[str, Any]]]):
    """返回一个按调用次序吐结果的 runner（附带记录收到的参数）。

    ★ 语义：第 n 次调用返回 ``raw_by_call[n]``；**次数用完之后一律返回空**。
    需要"连续多帧都有同一批检测"的用例（例如喂时序校验器），
    请把同一份 raw 重复若干次传进来，别只传一次。
    """
    calls: list[dict[str, Any]] = []

    def runner(frame, prompts, params):
        calls.append({"prompts": list(prompts), "params": dict(params), "shape": frame.shape[:2]})
        if len(calls) <= len(raw_by_call):
            return raw_by_call[len(calls) - 1]
        return []

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _det(
    config: dict[str, Any] | None = None, raw: list[dict[str, Any]] | None = None
) -> WorldDetector:
    """构造一个注入了假 runner 的 WorldDetector。

    第一个参数是**配置**，第二个是假 runner 要吐出的原始检测
    （沿用上游 runner 的中间形态：``{"prompt", "confidence", "bbox"}``）。
    这样契约测试、阈值测试、映射测试可以各自只关心自己那一维。
    """
    return WorldDetector(config or {}, runner=_fake_runner([raw or []]))


def _det_box(
    conf: float = 0.30,
    bbox: tuple[float, float, float, float] = (100, 80, 200, 160),
    prompt: str = "plastic bottle",
    config: dict[str, Any] | None = None,
) -> WorldDetector:
    raw = [{"prompt": prompt, "confidence": conf, "bbox": list(bbox)}]
    return _det(config, raw=raw)


# ----------------------------------------------------------------------
# 1) 输出契约：与 CvDetector 完全一致
# ----------------------------------------------------------------------
class TestContract:
    def test_keys_are_exactly_the_contract_keys(self) -> None:
        det = _det_box()
        dets = det.detect(_sea())
        assert len(dets) == 1
        assert set(dets[0]) == {"class", "confidence", "bbox"}, (
            "输出契约的键必须与 CvDetector 完全一致，多一个少一个都会打破下游"
        )

    def test_types(self) -> None:
        d = _det_box().detect(_sea())[0]
        assert isinstance(d["class"], str)
        assert isinstance(d["confidence"], float)
        assert isinstance(d["bbox"], list) and len(d["bbox"]) == 4
        assert all(isinstance(v, int) for v in d["bbox"]), (
            "bbox 必须是整数像素坐标（与 CvDetector 一致，浮点会破坏下游网格归并）"
        )

    def test_class_is_always_in_contract(self) -> None:
        for prompt in (*DEMO_DEFAULT_CLASSES, *CALIBRATION_PROMPTS, *DEMO_LIVE_PROMPTS,
                       "完全不认识的词 xyzzy"):
            det = _det_box(prompt=prompt)
            for d in det.detect(_sea()):
                assert d["class"] in CLASS_NAMES, f"{prompt} → {d['class']} 越出四类契约"

    def test_bbox_never_exceeds_frame(self) -> None:
        frame = _sea(size=(240, 320))
        for bbox in [(0, 0, 10, 10), (-50, -50, 100, 100), (300, 230, 500, 400)]:
            det = _det_box(bbox=bbox)
            for d in det.detect(frame):
                x1, y1, x2, y2 = d["bbox"]
                assert 0 <= x1 < x2 <= 320
                assert 0 <= y1 < y2 <= 240

    def test_degenerate_box_is_dropped(self) -> None:
        for bbox in [(100, 100, 100, 200), (100, 100, 200, 100), (200, 100, 100, 200)]:
            det = _det_box(bbox=bbox)
            assert det.detect(_sea()) == [], f"退化框应当被丢弃：{bbox}"
            assert det.stats()["dropped_bad_box"] >= 1

    def test_non_finite_values_are_dropped(self) -> None:
        raw = [{"prompt": "plastic bottle", "confidence": float("nan"),
                "bbox": [10, 10, 50, 50]}]
        det = _det(raw=raw)
        assert det.detect(_sea()) == []
        assert det.stats()["dropped_bad_box"] == 1

    def test_output_feeds_cv_detector_contract_shape_identically(self) -> None:
        """两个检测器的输出必须能走同一段下游代码。

        做法：各自产出一次检测，断言键集合与 bbox 类型一致。
        "两个模块各自都通过测试"不代表它们接得上 —— 这条就是接线的守卫。
        """
        world_det = _det_box(conf=0.9)
        world_out = world_det.detect(_sea())

        cv_det = CvDetector()
        frame = _sea()
        cv2.circle(frame, (320, 180), 18, (232, 235, 238), -1)
        for i in range(15):
            cv_det.detect(_sea(seed=i))
        cv_out = cv_det.detect(frame)

        assert world_out and cv_out, "两侧都应产出检测"
        assert set(world_out[0]) == set(cv_out[0])
        for out, name in ((world_out[0], "world"), (cv_out[0], "cv")):
            assert isinstance(out["bbox"], list) and len(out["bbox"]) == 4, name
            assert out["class"] in CLASS_NAMES, name

    def test_deterministic_order(self) -> None:
        """同一输入两次跑结果必须逐字节一致（下游对账依赖这一点）。"""
        raw = [
            {"prompt": "plastic bottle", "confidence": 0.5, "bbox": [300, 200, 340, 240]},
            {"prompt": "foam debris", "confidence": 0.5, "bbox": [100, 80, 200, 160]},
            {"prompt": "fishing net", "confidence": 0.9, "bbox": [10, 10, 60, 90]},
        ]
        first = _det(raw=raw).detect(_sea())
        second = _det(raw=raw).detect(_sea())
        assert first == second
        assert [d["confidence"] for d in first] == [0.9, 0.5, 0.5]

    def test_empty_frame_returns_empty(self) -> None:
        det = _det_box()
        assert det.detect(np.zeros((0, 0, 3), np.uint8)) == []
        assert det.stats()["calls"] == 0, "空帧不该计入调用数"

    def test_backend_name_is_declared(self) -> None:
        assert BACKEND_NAME == "world"
        assert WorldDetector.backend_name == "world"


# ----------------------------------------------------------------------
# 2) 标签映射（穷举）
# ----------------------------------------------------------------------
class TestLabelMap:
    def test_contract_classes_match_detector(self) -> None:
        """契约类别必须与 detector.CLASS_NAMES 逐项一致（第四处定义漂移守卫）。"""
        assert tuple(CONTRACT_CLASSES) == tuple(CLASS_NAMES)

    def test_every_mapping_value_is_a_contract_class(self) -> None:
        bad = {k: v for k, v in PROMPT_TO_CLASS.items() if v not in CONTRACT_CLASSES}
        assert not bad, f"映射表里有非契约类别：{bad}"

    def test_mapping_keys_are_normalized(self) -> None:
        """映射表的键必须已是归一化形式，否则永远匹配不到。"""
        for key in PROMPT_TO_CLASS:
            assert key == normalize(key), f"映射表键未归一化：{key!r}"

    def test_demo_default_classes_all_map_explicitly(self) -> None:
        for prompt in DEMO_DEFAULT_CLASSES:
            cls, kind = label_map.map_prompt(prompt)
            assert kind == MATCH_EXACT, f"{prompt} 没有明确归宿（回落 other 是缺陷，不是设计）"
            assert cls in CONTRACT_CLASSES

    def test_calibration_prompts_all_map_explicitly(self) -> None:
        for prompt in CALIBRATION_PROMPTS:
            _, kind = label_map.map_prompt(prompt)
            assert kind == MATCH_EXACT, f"{prompt} 没有明确归宿"

    def test_live_demo_prompts_all_map_explicitly(self) -> None:
        """现场加词用的词也必须都有归宿，否则演示时会"加了词却没反应"。"""
        for prompt in DEMO_LIVE_PROMPTS:
            _, kind = label_map.map_prompt(prompt)
            assert kind == MATCH_EXACT, f"{prompt} 没有明确归宿"

    def test_demo_default_classes_stay_in_sync_with_source_script(self) -> None:
        """★ 漂移守卫：本模块抄了一份演示脚本的 DEFAULT_CLASSES。

        上游哪天加了一个提示词而 label_map 没跟上，这条测试直接失败 ——
        而不是等到跑了 200 帧发现某类恒为 0 再回头查。
        """
        script = ROOT / "scripts" / "detect_marine_demo.py"
        assert script.exists(), f"演示脚本不在预期位置：{script}"

        tree = ast.parse(script.read_text(encoding="utf-8"))
        found: tuple[str, ...] | None = None
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "DEFAULT_CLASSES" in names:
                found = tuple(ast.literal_eval(node.value))
                break

        assert found is not None, f"{script} 里没找到 DEFAULT_CLASSES"
        assert found == DEMO_DEFAULT_CLASSES, (
            "演示脚本的提示词集已变化，label_map.DEMO_DEFAULT_CLASSES 需要同步：\n"
            f"  脚本：{found}\n  本模块：{DEMO_DEFAULT_CLASSES}"
        )

    def test_unknown_prompt_falls_back_but_is_observable(self) -> None:
        cls, kind = label_map.map_prompt("a completely unknown thing")
        assert (cls, kind) == ("other", MATCH_FALLBACK)
        assert not label_map.is_mapped("a completely unknown thing")
        assert unmapped_prompts(["plastic bottle", "???"]) == ["???"]

    def test_normalization_handles_case_and_spacing(self) -> None:
        assert normalize("  Plastic   Bottle ") == "plastic bottle"
        assert label_map.mapped_class("  PLASTIC   BOTTLE ") == "plastic"

    def test_all_seven_prompt_groups_have_a_home(self) -> None:
        """四类都必须有词可达 —— 否则某一类永远检不出来。"""
        reached = set(PROMPT_TO_CLASS.values())
        assert reached == set(CONTRACT_CLASSES), f"这些类别没有任何提示词可达：{set(CONTRACT_CLASSES) - reached}"


# ----------------------------------------------------------------------
# 3) 置信度标定：本通道绝不复用 cv 的 0.45
# ----------------------------------------------------------------------
class TestCalibration:
    def test_default_conf_is_not_the_temporal_gate(self) -> None:
        assert DEFAULT_WORLD_CONFIG["conf"] != CV_TEMPORAL_MIN_CONFIDENCE
        assert DEFAULT_WORLD_CONFIG["temporal_min_confidence"] != CV_TEMPORAL_MIN_CONFIDENCE

    def test_temporal_min_confidence_matches_configured_value(self) -> None:
        det = _det({"temporal_min_confidence": 0.12})
        assert det.temporal_min_confidence == pytest.approx(0.12)

    def test_temporal_min_confidence_falls_back_to_default(self) -> None:
        det = _det({})
        assert det.temporal_min_confidence == pytest.approx(
            DEFAULT_WORLD_CONFIG["temporal_min_confidence"]
        )

    def test_low_confidence_is_dropped_by_model_gate(self) -> None:
        gate = float(DEFAULT_WORLD_CONFIG["conf"])
        det = _det(
            raw=[
                {
                    "prompt": "plastic bottle",
                    "confidence": gate / 2,
                    "bbox": [10, 10, 60, 60],
                }
            ]
        )
        assert det.detect(_sea()) == []
        assert det.stats()["dropped_low_conf"] == 1

    def test_boundary_confidence_is_kept(self) -> None:
        gate = float(DEFAULT_WORLD_CONFIG["conf"])
        raw = [{"prompt": "plastic bottle", "confidence": gate, "bbox": [10, 10, 60, 60]}]
        det = _det(raw=raw)
        assert len(det.detect(_sea())) == 1, "恰好等于门限应当保留（与 CvDetector 的 >= 语义一致）"

    def test_world_output_is_dropped_by_cv_threshold_but_kept_by_world_threshold(self) -> None:
        """★ 守住原方案的致命口径错误。

        用历史实测里的量级（0.16）造一条检测：
        - 套 cv 通道的 0.45 门限 → 时序校验一条都确认不了；
        - 用 world 通道自己的门限 → 能确认。
        """
        observed_conf = 0.16  # 见 world_detector 模块头部的标定表（历史最高 0.184）
        raw = [{"prompt": "foam debris", "confidence": observed_conf,
                "bbox": [300, 170, 340, 200]}]

        world_gate = float(DEFAULT_WORLD_CONFIG["temporal_min_confidence"])

        def confirm_count(min_confidence: float) -> int:
            # 同一批检测连续出现若干帧（时序校验要求 min_hits 次命中才确认）
            det = WorldDetector(
                {"temporal_min_confidence": min_confidence},
                runner=_fake_runner([raw] * 8),
            )
            validator = TemporalValidator(
                window_frames=15, min_hits=3, grid_size=64,
                min_confidence=min_confidence, match_distance=60.0,
                max_misses=5, min_iou=0.25, decimation=1,
            )
            total = 0
            for i in range(8):
                total += len(validator.push(det.detect(_sea(seed=i))))
            return total

        assert confirm_count(world_gate) > 0, (
            "world 通道在自己的门限下必须能确认事件，否则这条通道等于没接上"
        )
        assert confirm_count(CV_TEMPORAL_MIN_CONFIDENCE) == 0, (
            "若 0.45 下也能确认，说明标定表已失效，请重新标定后再改这条断言"
        )


# ----------------------------------------------------------------------
# 4) 优雅降级（缺依赖不能炸主线，也不能静默）
# ----------------------------------------------------------------------
class TestDegradation:
    def test_unavailable_when_deps_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import clip_shim

        monkeypatch.setattr(
            clip_shim, "clip_available", lambda: (False, "未安装 torch（测试构造）")
        )
        det = WorldDetector({"clip_dir": "不存在的目录"})
        assert det.available is False
        assert det.detect(_sea()) == []
        assert det.last_error is not None and "torch" in det.last_error

    def test_unavailable_is_counted_not_silent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import clip_shim

        monkeypatch.setattr(clip_shim, "clip_available", lambda: (False, "缺依赖"))
        det = WorldDetector({})
        det.detect(_sea())
        det.detect(_sea())
        stats = det.stats()
        assert stats["unavailable_calls"] == 2
        assert stats["kept"] == 0
        assert stats["available"] is False

    def test_missing_weights_reports_actionable_reason(self, monkeypatch: pytest.MonkeyPatch,
                                                       tmp_path: Path) -> None:
        det = WorldDetector({"clip_dir": str(tmp_path / "nope")})
        # 不 mock 任何东西：本机若真的没装 torch，答案就是"未安装 torch"；
        # 装了 torch 但缺权重，答案必须指向目录与权重文件。
        ok, reason = det._probe_environment()
        assert ok is False
        assert reason, "不可用时必须给出原因，不能只返回 False"

    def test_failed_availability_check_populates_last_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """★ 只问 available 时，也必须能拿到原因。

        主程序在启动时先问 `available`，不可用就退出并打印 `last_error`。
        若 `available` 探到原因却没存下来，失败提示会变成"不可用：None"，
        等于没说 —— 这是实现时真实踩到的坑。
        """
        import clip_shim

        monkeypatch.setattr(
            clip_shim, "clip_available", lambda: (False, "未安装 torch（构造）")
        )
        det = WorldDetector({})
        assert det.available is False
        assert det.last_error is not None and "torch" in det.last_error

    def test_load_succeeds_with_injected_runner(self) -> None:
        det = WorldDetector({}, runner=_fake_runner([[]]))
        assert det.load() is True
        assert det.last_error is None

    def test_load_reports_model_build_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        det = WorldDetector({})
        # 依赖探测全绿（模拟"依赖齐、权重在"），但模型构建失败
        monkeypatch.setattr(det, "_probe_environment", lambda: (True, "构造：依赖齐备且权重就绪"))

        def boom() -> None:
            raise RuntimeError("模型加载失败：请把 torch 升到 2.6 以上")

        monkeypatch.setattr(det, "_build_ultralytics_runner", boom)
        assert det.load() is False
        assert det.last_error is not None and "torch" in det.last_error

    def test_available_can_be_true_while_load_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """★ 依赖齐备 ≠ 模型能加载。这是真实踩到的坑（2026-09-27）。

        实测环境：`torch 2.5.1` + `ultralytics 8.4.35`。
        `clip_shim.environment_report()` 三项全绿（依赖就绪 / 权重就绪 / ultralytics 就绪），
        但 ultralytics 因 CVE-2025-32434 拒绝在 torch<2.6 上加载 `.pt`
        （原文：*require users to upgrade torch to at least v2.6*）。

        结果就是：`available == True` 而每帧返回 0 检出 ——
        只看 `available` 就会把"根本没跑起来"记成"没有检到东西"。

        所以主程序与对照脚本都必须**两步都查**：先 available，再 load()。
        """
        det = WorldDetector({})
        monkeypatch.setattr(det, "_probe_environment", lambda: (True, "构造：依赖齐备且权重就绪"))

        def boom() -> None:
            raise RuntimeError("torch 版本过低，无法加载 .pt")

        monkeypatch.setattr(det, "_build_ultralytics_runner", boom)

        assert det.available is True, "依赖探测这一层应当是绿的（这正是坑所在）"
        assert det.load() is False, "load() 必须把这类失败拦下来"
        assert det.last_error is not None

    def test_runner_exception_does_not_break_the_loop(self) -> None:
        """单帧推理抛异常不能让整个取流循环崩掉。"""

        def exploding_runner(frame, prompts, params):
            raise RuntimeError("模拟的一次模型崩溃")

        det = WorldDetector({}, runner=exploding_runner)
        assert det.detect(_sea()) == []
        assert det.last_error is not None and "本帧推理失败" in det.last_error
        assert det.stats()["unavailable_calls"] == 1

    def test_failed_probe_is_not_retried_every_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """失败后不重复尝试（否则每帧都要 import 一次并打日志）。"""
        import clip_shim

        calls = {"n": 0}

        def counting_probe():
            calls["n"] += 1
            return False, "缺依赖"

        monkeypatch.setattr(clip_shim, "clip_available", counting_probe)
        det = WorldDetector({})
        for _ in range(5):
            det.detect(_sea())
        assert calls["n"] == 1, f"环境探测应只执行一次，实际 {calls['n']} 次"


# ----------------------------------------------------------------------
# 5) 现场加词
# ----------------------------------------------------------------------
class TestSetPrompts:
    def test_set_prompts_changes_class_mapping(self) -> None:
        det = _det(raw=[{"prompt": "fishing net", "confidence": 0.3, "bbox": [10, 10, 90, 30]}])
        assert det.detect(_sea())[0]["class"] == "fishing_gear"

        det.set_prompts(["foam buoy"])
        assert "foam buoy" in det.prompts

    def test_set_prompts_records_new_prompts(self) -> None:
        det = _det({})
        det.set_prompts(["plastic bag", "seaweed"])
        assert det.prompts == ("plastic bag", "seaweed")
        assert det.stats()["prompts"] == ["plastic bag", "seaweed"]

    def test_set_prompts_rejects_empty(self) -> None:
        det = _det({})
        with pytest.raises(ValueError, match="提示词不能为空"):
            det.set_prompts([])

    def test_unknown_prompt_is_counted_as_fallback(self) -> None:
        det = _det(raw=[{"prompt": "utterly unknown object", "confidence": 0.3,
                         "bbox": [10, 10, 60, 60]}])
        dets = det.detect(_sea())
        assert dets[0]["class"] == "other"
        assert det.stats()["fallback_other"] == 1

    def test_unmapped_prompts_surface_in_stats(self) -> None:
        det = _det({})
        det.set_prompts(["plastic bottle", "wonky prompt"])
        assert det.stats()["unmapped_prompts"] == ["wonky prompt"]

    def test_prompts_are_passed_to_runner(self) -> None:
        runner = _fake_runner([[]])
        det = WorldDetector({}, runner=runner)
        det.detect(_sea())
        assert runner.calls[0]["prompts"] == list(DEMO_DEFAULT_CLASSES)


# ----------------------------------------------------------------------
# 6) ROI
# ----------------------------------------------------------------------
class TestRoi:
    def test_empty_roi_keeps_everything(self) -> None:
        det = _det_box(bbox=(10, 10, 60, 60))
        assert len(det.detect(_sea())) == 1

    def test_box_center_outside_roi_is_dropped(self) -> None:
        roi = [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]  # 左半屏
        det = _det_box(bbox=(500, 100, 560, 160))  # 中心在右半屏
        det._roi = roi
        assert det.detect(_sea()) == []
        assert det.stats()["dropped_roi"] == 1

    def test_box_center_inside_roi_is_kept(self) -> None:
        roi = [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]
        det = _det_box(bbox=(20, 100, 80, 160))
        det._roi = roi
        assert len(det.detect(_sea())) == 1


# ----------------------------------------------------------------------
# 7) 配置对账与统计
# ----------------------------------------------------------------------
class TestConfigDrift:
    def test_defaults_match_edge_config_yaml(self) -> None:
        """★ 代码默认值必须与 edge/config.yaml 的 detector.world 段逐项对账。

        与 detector.py 的同类测试同一套路：参数写在 yaml 里、
        实例化时漏传，会导致运维改了配置却不生效 —— 且不报错。
        """
        import yaml

        cfg = yaml.safe_load((EDGE / "config.yaml").read_text(encoding="utf-8"))
        section = (cfg.get("detector") or {}).get("world")
        assert section, "edge/config.yaml 里没有 detector.world 段"

        mismatches: list[str] = []
        for key, default in DEFAULT_WORLD_CONFIG.items():
            if key not in section:
                mismatches.append(f"detector.world.{key} 在 config.yaml 中缺失")
                continue
            actual = section[key]
            if isinstance(default, bool) or isinstance(actual, bool):
                if bool(default) != bool(actual):
                    mismatches.append(f"detector.world.{key}: 代码 {default} ≠ 配置 {actual}")
            elif isinstance(default, (int, float)) and isinstance(actual, (int, float)):
                if abs(float(default) - float(actual)) > 1e-6:
                    mismatches.append(f"detector.world.{key}: 代码 {default} ≠ 配置 {actual}")
            elif isinstance(default, list) and isinstance(actual, list):
                if [str(x) for x in default] != [str(x) for x in actual]:
                    mismatches.append(f"detector.world.{key}: 代码与配置的提示词集不一致")

        assert not mismatches, "默认值与配置不一致：\n  " + "\n  ".join(mismatches)

    def test_backend_default_is_cv(self) -> None:
        """现网行为不变：默认必须走 cv 通道。"""
        import yaml

        cfg = yaml.safe_load((EDGE / "config.yaml").read_text(encoding="utf-8"))
        assert (cfg.get("detector") or {}).get("backend") == "cv"

    def test_cv_defaults_section_untouched(self) -> None:
        """cv 通道的参数集不能被本通道的改动带偏。"""
        from detector import DEFAULT_CONFIG

        assert "backend" not in DEFAULT_CONFIG, (
            "detector.DEFAULT_CONFIG 是 cv 通道的参数集；"
            "开放词汇参数放在 world 段，不要混进来"
        )
        assert "world" not in DEFAULT_CONFIG


# ----------------------------------------------------------------------
# 权重路径解析（★ 守一个真实踩到的坑）
# ----------------------------------------------------------------------
class TestWeightsPathResolution:
    """配置里的权重是裸文件名，而 ultralytics 按 **cwd** 找权重。

    后果（实测，不是推测）：从仓库根启动能找到本地文件；
    从 `edge/` 启动（`edge/main.py` 的常规用法）找不到 →
    ultralytics **静默转向联网下载**，重试 3 次、每次都卡在
    `curl: (56) schannel: server closed abruptly`，启动挂住 2 分钟以上，
    **且不抛出任何异常**。现场演示正是从 edge/ 启动的。

    所以解析必须与 cwd 无关，缺权重必须显式失败而不是静默下载。
    """

    def test_bare_filename_resolves_against_repo_root_not_cwd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        before = resolve_weights_path("yolov8s-worldv2.pt")
        monkeypatch.chdir(tmp_path)  # 换一个毫不相干的 cwd
        after = resolve_weights_path("yolov8s-worldv2.pt")
        assert before == after, "权重解析结果不得随当前工作目录变化"
        assert before == PROJECT_ROOT / "yolov8s-worldv2.pt"
        assert before.is_absolute()

    def test_absolute_path_is_preserved(self, tmp_path: Path) -> None:
        target = tmp_path / "some" / "weights.pt"
        assert resolve_weights_path(target) == target

    def test_shipped_weights_resolve_to_an_existing_file(self) -> None:
        """随包权重必须能在**默认配置下**被解析到一个真实存在的文件。

        这条同时守住两件事：权重没被挪走，以及默认 weights 没写成解析不到的值。
        """
        resolved = resolve_weights_path(DEFAULT_WORLD_CONFIG["weights"])
        assert resolved.exists(), (
            f"默认权重解析到 {resolved}，但该文件不存在 —— "
            "从 edge/ 启动时 ultralytics 会转去联网下载并把启动卡住"
        )

    def _stub_deps_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """把依赖与 CLIP 目录探测打成"全绿"，让权重检查成为唯一的失败点。"""
        import clip_shim

        monkeypatch.setattr(clip_shim, "clip_available", lambda: (True, "依赖齐备（构造）"))
        monkeypatch.setattr(clip_shim, "clip_dir_status", lambda _p: (True, "CLIP 就绪（构造）"))
        monkeypatch.setattr(clip_shim, "ultralytics_available", lambda: (True, "ultralytics 就绪（构造）"))

    def test_missing_weights_and_download_disabled_fails_explicitly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_deps_ok(monkeypatch)
        det = WorldDetector({"weights": "不存在的权重.pt", "allow_weights_download": False})
        ok, reason = det._probe_environment()
        assert ok is False
        # 原因必须给出**解析后的绝对路径**，否则运维不知道该把文件放哪
        assert str(resolve_weights_path("不存在的权重.pt")) in reason
        assert "allow_weights_download" in reason

    def test_missing_weights_with_download_enabled_is_not_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """显式允许下载时，可用性探测不应因为"本地没有"而拦下（改由 ultralytics 处理）。"""
        self._stub_deps_ok(monkeypatch)
        det = WorldDetector({"weights": "不存在的权重.pt", "allow_weights_download": True})
        ok, reason = det._probe_environment()
        assert ok is True, f"允许下载时不该在此拦下，实际原因：{reason}"

    def test_download_is_disabled_by_default(self) -> None:
        """默认必须禁止静默下载：现场网络不可预期，卡住比失败更糟。"""
        assert DEFAULT_WORLD_CONFIG["allow_weights_download"] is False


class TestCvChannelIsolation:
    """★ 线上只读挂载约束（2026-09-27 核查发现）。

    `docker-compose.prod.yml` 把整个 `./edge` 只读挂载进线上容器
    （`- ./edge:/edge:ro`），后端有 3 处 `from edge.detector.detector import CvDetector`。
    这意味着：**改动 edge/ 下的任何文件都会立刻被线上容器读到**。

    所以开放词汇通道必须做到"只增不改"：
    - 不碰 `detector.py`（cv 通道实现）；
    - 不在 `__init__.py` 里引用本模块 —— 否则后端那次 import 会连带把
      open-vocab 模块拉进平台进程，而它用的是扁平 import，在包上下文里会 ImportError，
      直接把线上后端拖挂。
    """

    def test_cv_detector_module_has_no_world_references(self) -> None:
        src = (HERE / "detector.py").read_text(encoding="utf-8")
        for token in ("world_detector", "WorldDetector", "DEFAULT_WORLD_CONFIG", "label_map"):
            assert token not in src, (
                f"detector.py 里出现了 {token}：cv 通道实现必须保持与开放词汇通道无关"
            )

    def test_package_init_does_not_pull_in_world_channel(self) -> None:
        init_src = (HERE / "__init__.py").read_text(encoding="utf-8")
        for token in ("world_detector", "label_map", "WorldDetector"):
            assert token not in init_src, (
                f"__init__.py 里出现了 {token}：后端的 `from edge.detector.detector import "
                "CvDetector` 会被它连带拉起，务必保持 __init__.py 不引用本模块"
            )


class TestStatsObservability:
    def test_stats_expose_every_drop_reason_separately(self) -> None:
        # ★ 显式给配置，不吃随包默认值：
        #   本测试要构造"低于门限的框"，而"低多少算低"取决于 conf。
        #   原先这里用固定 0.02 去对比当时的默认 0.10，默认值一改
        #   （0.10 → 0.02）这条 0.02 就不再低于门限，断言静默失效。
        #   把门限钉在测试里，测的才是"丢框计数"这件事本身。
        det = _det({"conf": 0.10, "temporal_min_confidence": 0.10}, raw=[
            {"prompt": "plastic bottle", "confidence": 0.02, "bbox": [10, 10, 60, 60]},
            {"prompt": "plastic bottle", "confidence": 0.30, "bbox": [10, 10, 10, 60]},
            {"prompt": "unknown thing", "confidence": 0.30, "bbox": [10, 10, 60, 60]},
            {"prompt": "foam debris", "confidence": 0.30, "bbox": [10, 10, 60, 60]},
        ])
        dets = det.detect(_sea(seed=1))
        stats = det.stats()
        assert len(dets) == 2
        assert stats["dropped_low_conf"] == 1
        assert stats["dropped_bad_box"] == 1
        assert stats["fallback_other"] == 1
        assert stats["raw"] == 4
        assert stats["kept"] == 2

    def test_default_conf_keeps_every_contract_class_reportable(self) -> None:
        """默认门限必须低于实测最紧类别的最高分。

        ★ 这条守的是一个真实发生过的缺陷：默认 conf 原为 0.10，
        而实测里 fishing_gear/plastic/foam 三类的最高分都低于 0.10 ——
        即在该门限下**这个通道永远只会输出 other**，
        现场"加一个词看它检出来"的演示必然毫无反应，
        而现象只是"加词没效果"，看不出是门限造成的。

        数据来源：artifacts/metrics/vision_backends_latest.json 的
        threshold_probe（5 张 4K 真实照片，门限压到 0.001）。
        它是**这批图上的观测**，不是真值；但足以约束默认门限：
        默认值一旦被调到 0.026 以上，本测试立刻失败并要求重新取证。
        """
        measured_class_top = {
            "other": 0.5824, "fishing_gear": 0.0514,
            "plastic": 0.0355, "foam": 0.0260,
        }
        conf = float(DEFAULT_WORLD_CONFIG["conf"])
        unreachable = sorted(c for c, top in measured_class_top.items() if top < conf)
        assert not unreachable, (
            f"默认 conf={conf} 会让这些契约类别永远不可报（实测最高分低于门限）："
            f"{unreachable}。这正是本次修掉的缺陷（原 0.10，四类里只有 other 可报）。"
        )

    def test_reset_clears_counters_but_keeps_prompts(self) -> None:
        det = _det_box()
        det.detect(_sea())
        det.set_prompts(["plastic bag"])
        det.reset()
        stats = det.stats()
        assert stats["calls"] == 0 and stats["kept"] == 0
        assert det.prompts == ("plastic bag",), "reset 不该动提示词"

    def test_stats_include_calibration_values(self) -> None:
        stats = _det({}).stats()
        assert stats["conf"] == pytest.approx(DEFAULT_WORLD_CONFIG["conf"])
        assert stats["temporal_min_confidence"] == pytest.approx(
            DEFAULT_WORLD_CONFIG["temporal_min_confidence"]
        )
