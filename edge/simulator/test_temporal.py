"""边缘感知模拟器 —— 时序校验逻辑测试。

不需要 MQTT Broker，纯逻辑验证。

运行：
    python -m pytest test_temporal.py -v
    # 或直接
    python test_temporal.py
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from simulator import TemporalValidator  # noqa: E402


def _det(cls: str = "foam", conf: float = 0.9, bbox: list[int] | None = None) -> dict:
    return {"class": cls, "confidence": conf, "bbox": bbox or [400, 300, 460, 340]}


# 说明：校验器默认 decimation=4（抽帧推理），多数逻辑测试关心的是
# 「抽中的那一帧怎么判」，所以统一用 decimation=1 关掉抽帧，把逻辑与
# 节奏解耦。抽帧本身的行为由 test_decimation_suppresses_noise 单独覆盖。
NO_DECIMATION = 1

# ★ 从实现里取默认值，测试不硬写数字。
#   历史教训：测试曾经硬写 max_misses=6，而文档写 5、代码默认也是 6 ——
#   三方不一致却全部"通过"，因为测试根本没去核对默认值，只是复述了一个数。
#   现在只要实现改了默认值而文档没跟，test_defaults_match_design_doc 立刻报错。
DEFAULT_MAX_MISSES = inspect.signature(TemporalValidator.__init__).parameters[
    "max_misses"
].default


def test_single_frame_never_confirms() -> None:
    """单帧检测不应被确认为事件 —— 这是降误报的第一原则。"""
    v = TemporalValidator(
        window_frames=15, min_hits=3, max_misses=DEFAULT_MAX_MISSES, decimation=NO_DECIMATION
    )
    out = v.push([_det()])
    assert out == [], "单帧就确认，说明时序校验没起作用"
    print("  ✓ 单帧不确认")


def test_consecutive_hits_confirm() -> None:
    """连续 min_hits 帧命中同一目标 → 确认。"""
    v = TemporalValidator(
        window_frames=15, min_hits=3, max_misses=DEFAULT_MAX_MISSES, decimation=NO_DECIMATION
    )
    results = []
    for i in range(5):
        # 同一目标逐帧微小漂移
        out = v.push([_det(bbox=[400 + i * 3, 300 + i * 2, 460 + i * 3, 340 + i * 2])])
        results.append(len(out))

    assert sum(results) == 1, f"应恰好确认 1 次，实际 {sum(results)} 次"
    assert results[2] == 1, f"应在第 3 帧确认，实际第 {results.index(1) + 1} 帧"
    print("  ✓ 连续 3 帧命中后确认，且只确认一次")


def test_decimation_suppresses_noise() -> None:
    """抽帧推理下：稳定目标仍被确认，闪现噪声抑制得更干净。

    这是真实边缘盒的语义 —— 模型不是逐帧跑，而是隔几帧推一次。
    抽帧对噪声是双重打击：闪现检测要么被跳过，要么只被抽中一次，
    凑不满 min_hits；而持续目标在抽中的帧里照样连续命中。
    """
    v = TemporalValidator(window_frames=15, min_hits=3, max_misses=DEFAULT_MAX_MISSES, decimation=4)

    for i in range(60):
        frame: list[dict] = []
        # 持续目标：每帧都在（抽中时必命中）
        frame.append(_det(bbox=[500 + i % 5, 400 + i % 4, 570 + i % 5, 450 + i % 4]))
        # 瞬态噪声：随机位置，前后帧对不上
        for k in range(4):
            x = 100 + (i * 101 + k * 233) % 1600
            y = 80 + (i * 67 + k * 149) % 900
            frame.append(_det(conf=0.55, bbox=[x, y, x + 60, y + 45]))

        v.push(frame)
        v.sweep(v.decimation)

    # 只有 1 个目标（持续目标）被确认，瞬态噪声全灭
    assert v.stats["confirmed"] == 1, (
        f"抽帧模式下应只确认持续目标 1 个，实际 {v.stats['confirmed']} 个"
    )
    print("  ✓ 抽帧推理：持续目标确认、瞬态噪声全灭")


def test_transient_noise_suppressed() -> None:
    """每帧随机落点的瞬态噪声永远不应被确认。"""
    v = TemporalValidator(
        window_frames=15, min_hits=3, max_misses=DEFAULT_MAX_MISSES, decimation=NO_DECIMATION
    )
    confirmed = 0
    for i in range(40):
        # 每帧完全不同的位置 —— 模拟浪花反光
        x = 100 + i * 37 % 1700
        y = 80 + i * 53 % 950
        confirmed += len(v.push([_det(bbox=[x, y, x + 50, y + 40])]))

    assert confirmed == 0, f"瞬态噪声被误确认 {confirmed} 次，校验失效"
    print("  ✓ 40 帧随机噪声 0 次确认")


def test_low_confidence_rejected() -> None:
    """置信度低于阈值的目标直接丢弃，不进入跟踪。"""
    v = TemporalValidator(min_hits=3, min_confidence=0.45, decimation=NO_DECIMATION)
    for _ in range(10):
        v.push([_det(conf=0.30)])

    assert v.stats["confirmed"] == 0
    assert v.stats["low_confidence"] == 10
    print("  ✓ 低置信度直接丢弃")


def test_different_class_not_matched() -> None:
    """类别不同不应被当作同一目标（避免泡沫与渔具串号）。"""
    v = TemporalValidator(min_hits=3, max_misses=DEFAULT_MAX_MISSES, decimation=NO_DECIMATION)
    for i in range(10):
        cls = "foam" if i % 2 == 0 else "fishing_gear"
        out = v.push([_det(cls=cls, bbox=[400, 300, 460, 340])])

    # 每个类别各命中 5 次，但交替出现 → 各自都会累计到 3 次
    # 关键是不能出现「泡沫的跟踪器被渔具检测续命」导致计数虚高
    assert v.stats["confirmed"] <= 2, "类别隔离失效"
    print("  ✓ 类别隔离正确")


def test_track_decay_removes_stale() -> None:
    """长时间不再出现的目标应被淘汰，不占用跟踪池。

    注意衰减由 sweep() 驱动（不是 push）—— 这样在没有抽帧时，
    miss 才等价于"经过的帧数"。decimation=1 时每帧都 sweep，语义最直观。
    """
    v = TemporalValidator(min_hits=3, max_misses=3, decimation=NO_DECIMATION)
    v.push([_det()])
    assert v.stats["tracked"] == 1

    # 连续 5 帧空画面（每帧都要 sweep 才推进衰减）
    for _ in range(5):
        v.push([])
        v.sweep(v.decimation)

    assert v.stats["tracked"] == 0, f"过期目标未淘汰，仍有 {v.stats['tracked']} 个"
    print("  ✓ 目标按 miss 衰减淘汰")


def test_noise_filtered_by_confirmation_rate() -> None:
    """混合场景：持续目标 + 瞬态噪声，噪声抑制率应显著。"""
    v = TemporalValidator(
        window_frames=15, min_hits=3, max_misses=DEFAULT_MAX_MISSES, decimation=NO_DECIMATION
    )

    confirmed_total = 0
    raw_total = 0
    for i in range(60):
        frame = []
        # 持续目标（每帧都在，微漂移）
        frame.append(_det(bbox=[500 + i % 5, 400 + i % 4, 570 + i % 5, 450 + i % 4]))
        # 瞬态噪声（随机位置）
        for k in range(4):
            x = 100 + (i * 101 + k * 233) % 1600
            y = 80 + (i * 67 + k * 149) % 900
            frame.append(_det(conf=0.55, bbox=[x, y, x + 60, y + 45]))

        raw_total += len(frame)
        confirmed_total += len(v.push(frame))

    assert confirmed_total == 1, f"应只确认 1 个持续目标，实际 {confirmed_total}"
    print(f"  ✓ 混合场景：{raw_total} 条原始检测 → 仅 {confirmed_total} 个确认事件")


def test_suppression_ratio_in_range() -> None:
    """抑制率必须恒在 0~100% —— 守住统计口径的单位一致性。

    历史 bug：早期实现把「淘汰的 track 数」除以「检测数」，
    两者单位不同，算出来 140.9%。这个断言就是那道防线。
    """
    v = TemporalValidator(window_frames=15, min_hits=3, max_misses=DEFAULT_MAX_MISSES, decimation=4)

    for i in range(60):
        frame = [_det(bbox=[500 + i % 5, 400 + i % 4, 570 + i % 5, 450 + i % 4])]
        for k in range(4):
            x = 100 + (i * 101 + k * 233) % 1600
            y = 80 + (i * 67 + k * 149) % 900
            frame.append(_det(conf=0.55, bbox=[x, y, x + 60, y + 45]))
        v.push(frame)
        v.sweep(v.decimation)

    st = v.stats
    ratio = st["suppressed"] / st["fed"] if st["fed"] else 0
    assert 0.0 <= ratio <= 1.0, f"抑制率越界：{ratio:.1%}（统计口径又混单位了）"
    assert st["suppressed"] + st["absorbed"] == st["fed"], (
        f"suppressed({st['suppressed']}) + absorbed({st['absorbed']}) "
        f"应等于 fed({st['fed']})"
    )
    print(f"  ✓ 抑制率 {ratio:.1%} 落在 0~100%，且 fed = absorbed + suppressed")


def test_defaults_match_design_doc() -> None:
    """★ 代码默认值必须与 docs/software-design.md §2.3 参数表一致。

    这条测试守的是「参数漂移」这一类缺陷 —— 它不会让任何功能报错，
    只是让文档和代码各说各话，等到有人照文档去调参才发现改了没用。

    历史事实：`max_misses` 曾在文档里写 5、代码里写 6，长期无人发现。
    """
    doc = Path(__file__).resolve().parents[2] / "docs" / "software-design.md"
    if not doc.exists():
        print("  · 跳过（找不到设计文档，可能是单独拷贝了本目录）")
        return

    text = doc.read_text(encoding="utf-8")
    sig = inspect.signature(TemporalValidator.__init__).parameters

    # 参数表形如：| `max_misses` | 5 | 丢失 5 帧（0.6 秒）后淘汰，容忍短暂遮挡 |
    wanted = [
        "window_frames",
        "min_hits",
        "min_confidence",
        "match_distance",
        "min_iou",
        "max_misses",
        "decimation",
    ]
    mismatched: list[str] = []
    for name in wanted:
        # 文档里的值可能带单位（如 `60 px`），所以数字后允许跟非竖线字符
        m = re.search(rf"\|\s*`{name}`\s*\|\s*([0-9.]+)\s*[^|]*\|", text)
        if not m:
            mismatched.append(f"{name}: 文档参数表中未找到该行")
            continue
        doc_val = float(m.group(1))
        code_val = float(sig[name].default)
        if abs(doc_val - code_val) > 1e-9:
            mismatched.append(f"{name}: 文档={doc_val} 代码={code_val}")

    assert not mismatched, "代码与设计文档参数不一致：\n    " + "\n    ".join(mismatched)
    print(f"  ✓ {len(wanted)} 个门限参数与设计文档逐项一致")


def test_config_yaml_declares_all_thresholds() -> None:
    """★ config.yaml 必须显式声明全部门限键。

    早期版本实例化校验器时漏传 match_distance / min_iou，
    导致在配置里改了也不生效（静默回落到类默认值）。
    这条测试保证「配置能表达」与「代码会读取」对齐。
    """
    cfg = Path(__file__).with_name("config.yaml")
    if not cfg.exists():
        print("  · 跳过（找不到 config.yaml）")
        return

    text = cfg.read_text(encoding="utf-8")
    # 借 simulator 的 yaml 依赖来解析，避免再写一遍
    try:
        import yaml
    except ImportError:
        print("  · 跳过（未安装 pyyaml）")
        return

    cfg_data = yaml.safe_load(text) or {}
    temporal = cfg_data.get("temporal") or {}

    required = [
        "window_frames",
        "min_hits",
        "grid_size",
        "min_confidence",
        "match_distance",
        "min_iou",
        "max_misses",
        "decimation",
    ]
    missing = [k for k in required if k not in temporal]
    assert not missing, (
        f"config.yaml 的 temporal 段缺少这些键：{missing}\n"
        f"    （漏配的项会静默回落到代码默认值，改配置不生效）"
    )

    # 配置值也须与实现默认值一致 —— 否则「不配就能跑」的假设不成立
    sig = inspect.signature(TemporalValidator.__init__).parameters
    drift = [
        f"{k}: 配置={temporal[k]} 代码默认={sig[k].default}"
        for k in required
        if k in sig and abs(float(temporal[k]) - float(sig[k].default)) > 1e-9
    ]
    assert not drift, "config.yaml 与代码默认值不一致：\n    " + "\n    ".join(drift)
    print(f"  ✓ config.yaml 声明了全部 {len(required)} 个门限键，且与代码默认值一致")


def main() -> int:
    tests = [
        test_single_frame_never_confirms,
        test_consecutive_hits_confirm,
        test_decimation_suppresses_noise,
        test_transient_noise_suppressed,
        test_low_confidence_rejected,
        test_different_class_not_matched,
        test_track_decay_removes_stale,
        test_noise_filtered_by_confirmation_rate,
        test_suppression_ratio_in_range,
        test_defaults_match_design_doc,
        test_config_yaml_declares_all_thresholds,
    ]

    print("=" * 62)
    print("  时序校验逻辑测试")
    print("=" * 62)

    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            print(f"  ✗ {test.__name__}: {exc}")
            failed += 1
        except Exception as exc:   # noqa: BLE001
            print(f"  ✗ {test.__name__}: 异常 {type(exc).__name__}: {exc}")
            failed += 1

    print("=" * 62)
    if failed:
        print(f"  结果：{len(tests) - failed}/{len(tests)} 通过，{failed} 失败")
        return 1
    print(f"  结果：全部 {len(tests)} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
