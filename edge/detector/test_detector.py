"""OpenCV 检测器测试 —— 用合成帧验证，不需要真实摄像头与视频。

为什么能纯合成：检测器的输入是一帧 BGR 数组，输出是契约格式的检测列表。
只要能构造出"海面背景 + 已知目标"，就能断言它检出了什么。
这比"等出海采集再验证"快两个数量级，也是 OpenCV 路线相对 YOLO
最大的工程优势之一：**不依赖数据也能证明它在工作**。

运行：
    python -m pytest test_detector.py -v
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2", reason="需要 opencv-python-headless")

HERE = Path(__file__).resolve().parent
EDGE = HERE.parent
ROOT = EDGE.parent

sys.path.insert(0, str(HERE))
sys.path.insert(0, str(EDGE / "simulator"))

from detector import CLASS_NAMES, DEFAULT_CONFIG, CvDetector  # noqa: E402
from simulator import TemporalValidator  # noqa: E402

# 时序校验的门限（时序过滤器的第一道闸，检测器给出的分数必须能跨过它）
MIN_CONFIDENCE = 0.45


# ----------------------------------------------------------------------
# 合成帧
# ----------------------------------------------------------------------
def _sea(seed: int = 0, size: tuple[int, int] = (360, 640)) -> np.ndarray:
    """灰蓝海面背景（带纹理噪声）。

    亮度刻意压在 V<150：真实海面在这个亮度以下，
    否则背景本身会被"白亮"颜色通道当成泡沫 —— 那是最大的误报源。
    """
    rng = np.random.default_rng(seed)
    base = np.zeros((size[0], size[1], 3), np.uint8)
    base[:] = (110, 100, 82)  # BGR：偏灰蓝
    noise = rng.normal(0, 4, base.shape)
    return np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def _with_foam(seed: int = 0, center: tuple[int, int] = (320, 180)) -> np.ndarray:
    frame = _sea(seed)
    cv2.circle(frame, center, 18, (232, 235, 238), -1)  # V≈238：白但没过曝
    return frame


def _with_plastic(seed: int = 0) -> np.ndarray:
    frame = _sea(seed)
    cv2.rectangle(frame, (420, 150), (480, 210), (220, 90, 60), -1)  # 蓝色块
    return frame


def _with_fishing_gear(seed: int = 0) -> np.ndarray:
    frame = _sea(seed)
    cv2.rectangle(frame, (150, 200), (240, 214), (55, 50, 45), -1)  # 暗色细长
    return frame


def _with_glare(seed: int = 0) -> np.ndarray:
    frame = _sea(seed)
    cv2.rectangle(frame, (500, 300), (530, 306), (255, 255, 255), -1)  # 极亮细条
    return frame


def _warm(detector: CvDetector, frames: int = 15) -> None:
    """让背景模型先学会"空海面"。"""
    for i in range(frames):
        detector.detect(_sea(seed=i))


def _first(detector: CvDetector, frame: np.ndarray) -> dict:
    dets = detector.detect(frame)
    assert dets, "期望至少检出一个目标，实际为空"
    return dets[0]


# ----------------------------------------------------------------------
# 基础检测行为
# ----------------------------------------------------------------------
class TestDetection:
    def test_calm_sea_detects_nothing(self) -> None:
        """平静海面（背景建模之后）不应有检测。"""
        det = CvDetector()
        _warm(det)
        assert det.detect(_sea(seed=99)) == [], "空海面被检出了目标 —— 误报起点"
        print("  ✓ 平静海面零检出")

    def test_foam_blob_is_detected(self) -> None:
        det = CvDetector()
        _warm(det)
        d = _first(det, _with_foam())
        assert d["class"] == "foam", f"白团被判成 {d['class']}"
        assert d["confidence"] >= MIN_CONFIDENCE, "置信度低于时序门限，会被直接丢弃"
        print(f"  ✓ 泡沫：class={d['class']} conf={d['confidence']}")

    def test_colorful_plastic_is_detected(self) -> None:
        det = CvDetector()
        _warm(det)
        d = _first(det, _with_plastic())
        assert d["class"] == "plastic", f"彩色块被判成 {d['class']}"
        print(f"  ✓ 塑料：class={d['class']} conf={d['confidence']}")

    def test_dark_elongated_is_fishing_gear(self) -> None:
        det = CvDetector()
        _warm(det)
        d = _first(det, _with_fishing_gear())
        assert d["class"] == "fishing_gear", f"暗色细长物被判成 {d['class']}"
        print(f"  ✓ 渔具：class={d['class']} conf={d['confidence']}")

    def test_bbox_is_valid_and_inside_frame(self) -> None:
        det = CvDetector()
        _warm(det)
        frame = _with_foam()
        h, w = frame.shape[:2]
        for d in det.detect(frame):
            x1, y1, x2, y2 = d["bbox"]
            assert len(d["bbox"]) == 4
            assert 0 <= x1 < x2 <= w, f"bbox 横向越界：{d['bbox']}"
            assert 0 <= y1 < y2 <= h, f"bbox 纵向越界：{d['bbox']}"
        print("  ✓ bbox 全部落在画面内且有效")

    def test_class_is_always_in_contract(self) -> None:
        """输出的类别必须落在四类别契约内 —— 否则平台侧枚举会拒收。"""
        det = CvDetector()
        _warm(det)
        for frame in (_with_foam(), _with_plastic(), _with_fishing_gear(), _with_glare()):
            for d in det.detect(frame):
                assert d["class"] in CLASS_NAMES, f"非法类别 {d['class']}"
        print("  ✓ 类别全部在契约内")

    def test_tiny_noise_is_ignored(self) -> None:
        """水花级别的小噪点不应成为候选。"""
        det = CvDetector()
        _warm(det)
        frame = _sea(seed=5)
        cv2.circle(frame, (300, 200), 3, (240, 240, 240), -1)  # 面积 ≈ 28 < min_area
        assert det.detect(frame) == [], "小水花被当成目标"
        print("  ✓ 小噪点被过滤")

    def test_static_target_still_detected(self) -> None:
        """★ 停着不动的泡沫也必须持续检出。

        这是"颜色通道"存在的理由：只用背景减除时，一片停在镜头前的
        泡沫会在几十帧后被学成背景，从此永不报警 —— 而泡沫是本项目
        最该抓的类别。这个测试锁住这个行为，防止后人"优化"掉颜色通道。
        """
        det = CvDetector()
        _warm(det)
        for i in range(60):
            out = det.detect(_with_foam(seed=i))
            assert out and out[0]["class"] == "foam", f"第 {i} 帧丢失静止目标"
        print("  ✓ 静止目标连续 60 帧仍在检出")

    def test_glare_scores_below_foam(self) -> None:
        """海面反光必须显著低于真实泡沫的置信度。

        注意这是**相对**断言而不是"反光必须被压到 0.45 以下"：
        高光与白泡沫在单帧特征上高度重合，真把它们分开的是时序
        （高光闪现、凑不满 min_hits）。这里只保证打分把它们排开。
        """
        det = CvDetector()
        _warm(det)
        foam = _first(det, _with_foam())["confidence"]
        det2 = CvDetector()
        _warm(det2)
        glare = _first(det2, _with_glare())["confidence"]
        assert foam - glare > 0.10, f"反光没被压下去：foam={foam} glare={glare}"
        print(f"  ✓ 反光打分被压低：foam={foam} glare={glare}")

    def test_roi_excludes_outside_target(self) -> None:
        """ROI 之外的目标不参与检测（排除天空、岸边建筑、固定缆绳）。"""
        # 只在左半幅检测
        roi = [[0.0, 0.0], [0.6, 0.0], [0.6, 1.0], [0.0, 1.0]]
        det = CvDetector(roi=roi)
        _warm(det)
        assert det.detect(_with_foam(center=(550, 180))) == [], "ROI 外的目标混进来了"
        assert det.detect(_with_foam(center=(200, 180))), "ROI 内的目标丢了"
        print("  ✓ ROI 生效")

    def test_confidence_within_declared_bounds(self) -> None:
        det = CvDetector()
        _warm(det)
        low = DEFAULT_CONFIG["confidence"]["min"]
        high = DEFAULT_CONFIG["confidence"]["max"]
        for frame in (_with_foam(), _with_plastic(), _with_fishing_gear()):
            for d in det.detect(frame):
                assert low <= d["confidence"] <= high, f"置信度越界：{d['confidence']}"
        print("  ✓ 置信度落在 [min, max] 内")


# ----------------------------------------------------------------------
# 契约一致性（第四处类别定义不能漂移）
# ----------------------------------------------------------------------
class TestContractConsistency:
    def test_class_names_match_ai_server(self) -> None:
        """与 backend/app/services/ai/server.py 的 CLASS_NAMES 逐项一致。"""
        path = ROOT / "backend" / "app" / "services" / "ai" / "server.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found: list[str] | None = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "CLASS_NAMES":
                        found = [e.value for e in node.value.elts]  # type: ignore[union-attr]
        assert found is not None, "没解析到 CLASS_NAMES —— 锚点失效，守卫已空转"
        assert found == CLASS_NAMES, f"AI 服务 {found} ≠ 检测器 {CLASS_NAMES}"
        print(f"  ✓ 与 AI 服务一致：{CLASS_NAMES}")

    def test_class_names_match_dataset_yaml(self) -> None:
        """与 ml/configs/seasight.yaml 的 names（即模型类别索引）一致。"""
        import yaml  # noqa: PLC0415

        cfg = yaml.safe_load((ROOT / "ml" / "configs" / "seasight.yaml").read_text(encoding="utf-8"))
        names = cfg["names"]
        assert len(names) == len(CLASS_NAMES), f"yaml 里 {len(names)} 类，代码里 {len(CLASS_NAMES)} 类"
        for idx, name in sorted(names.items()):
            assert CLASS_NAMES[idx] == name, f"索引 {idx} 应为 {name}，实际 {CLASS_NAMES[idx]}"
        print("  ✓ 与数据集 yaml 一致（顺序即模型类别索引）")

    def test_defaults_match_edge_config_yaml(self) -> None:
        """★ 代码默认值必须与 edge/config.yaml 逐项对账。

        项目踩过的坑：参数写在 yaml 里，但实例化时漏传，导致运维改了
        配置却不生效 —— 且不报错。这里把每个默认值都和 yaml 对一遍。
        """
        import yaml  # noqa: PLC0415

        cfg = yaml.safe_load((EDGE / "config.yaml").read_text(encoding="utf-8"))
        section = cfg.get("detector")
        assert section, "config.yaml 里没有 detector 段"

        mismatches: list[str] = []

        def walk(default: object, actual: object, path: str) -> None:
            if isinstance(default, dict):
                for key, value in default.items():
                    if not isinstance(actual, dict) or key not in actual:
                        mismatches.append(f"{path}.{key} 在 config.yaml 中缺失")
                        continue
                    walk(value, actual[key], f"{path}.{key}")
            elif isinstance(actual, bool) or isinstance(default, bool):
                if bool(default) != bool(actual):
                    mismatches.append(f"{path}: 代码 {default} ≠ 配置 {actual}")
            elif isinstance(default, (int, float)) and isinstance(actual, (int, float)):
                if abs(float(default) - float(actual)) > 1e-6:
                    mismatches.append(f"{path}: 代码 {default} ≠ 配置 {actual}")

        walk(DEFAULT_CONFIG, section, "detector")
        assert not mismatches, "默认值与配置不一致：\n  " + "\n  ".join(mismatches)
        print(f"  ✓ {len(DEFAULT_CONFIG)} 个配置段与 config.yaml 对账通过")


# ----------------------------------------------------------------------
# 接线：检测器 → 时序校验器
# ----------------------------------------------------------------------
class TestWiring:
    def test_detector_output_feeds_temporal_validator(self) -> None:
        """★ 检测器的输出必须能直接喂给时序校验器并被确认。

        这是"实现了 N 步、只接上了 N-2 步"那类缺陷的守卫：
        两个模块各自都通过测试，不代表它们接得上。
        """
        det = CvDetector()
        _warm(det)
        validator = TemporalValidator(decimation=1)  # 关抽帧，只看接线是否通

        confirmed: list[dict] = []
        for i in range(6):
            frame = _sea(seed=100 + i)
            cv2.circle(frame, (320 + i * 4, 180 + i * 3), 18, (232, 235, 238), -1)
            confirmed.extend(validator.push(det.detect(frame)))

        assert confirmed, "检测器与时序校验器没接上：连续 6 帧未确认任何事件"
        assert confirmed[0]["class"] == "foam"
        assert confirmed[0]["confidence"] >= MIN_CONFIDENCE
        print(f"  ✓ 端到端接通：{len(confirmed)} 条确认事件（class={confirmed[0]['class']}）")

    def test_single_glare_frame_never_confirms(self) -> None:
        """反光只在单帧闪现 → 不该被确认为事件（时序压制生效）。"""
        det = CvDetector()
        _warm(det)
        validator = TemporalValidator(decimation=1)
        validator.push(det.detect(_with_glare()))
        for i in range(10):
            validator.push(det.detect(_sea(seed=200 + i)))
        # stats 是 property，不是方法
        assert validator.stats["confirmed"] == 0, "单帧反光被确认成事件了"
        print("  ✓ 单帧反光未被确认")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
