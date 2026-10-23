"""放置入槽（料槽）回归测试 —— 无 ROS、无相机、无硬件。

这份测试锁的是**验收动作本身**：平台派单后 → 识别静态塑料袋 → 夹取 →
放进臂座旁边的料槽。抓取链路里唯一"位置固定"的一步就是放置点，也是
最容易在演示现场翻车的一步（没示教 / 不可达 / 悄悄丢在原地），所以：

* 放置点怎么给（``coord`` / ``pose`` / 配置兜底）必须都能走通；
* **没有放置点必须明确失败**，不允许"夹住了就算成功"；
* 放置动作序列必须真的经过料槽坐标，而不是原地松爪。

用假 io / 假检测器，几秒内跑完整条六态闭环。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
GRASP = HERE.parent
EDGE = GRASP.parent
for _p in (str(EDGE), str(GRASP)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from grasp.calibration import demo_calibration, describe_place  # noqa: E402
from grasp.grasp_loop import GraspLoop                          # noqa: E402

PLANE_Z = 0.005
HOPPER = (0.16, 0.02)          # 右舷料槽（配置里的例子）


# ----------------------------------------------------------------------
# 假件
# ----------------------------------------------------------------------
class FakeDetection:
    cls = "plastic_bag"

    def __init__(self, center: tuple[float, float], conf: float = 0.87) -> None:
        self.center = center
        self.grasp_point = center
        self.conf = conf
        self.x1, self.y1 = center[0] - 30, center[1] - 20
        self.x2, self.y2 = center[0] + 30, center[1] + 20

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    def to_dict(self) -> dict[str, Any]:
        return {"cls": self.cls, "conf": self.conf, "center": list(self.center)}


class FakeIo:
    """记录所有动作的假硬件层。"""

    def __init__(self) -> None:
        self.mock = True
        self.grabbed = False
        self.moves: list[tuple[float, float, float]] = []
        self.move_pitches: list[float | None] = []
        self.servo_batches: list[list[tuple[int, int]]] = []
        self.events: list[str] = []

    # 画面：全黑 BGR（HSV 饱和度 0），保证决策层的颜色校验不误杀
    def latest_frame(self) -> Any:
        return np.zeros((480, 640, 3), dtype=np.uint8)

    def move_to(self, x: float, y: float, z: float, pitch: float | None = None,
                **_: Any) -> bool:
        self.moves.append((float(x), float(y), float(z)))
        self.move_pitches.append(None if pitch is None else float(pitch))
        self.events.append("move")
        return True

    def set_servos(self, duration_ms: int, pairs: list[tuple[int, int]]) -> None:
        self.servo_batches.append([(int(a), int(b)) for a, b in pairs])
        self.events.append("servos")

    def gripper_open(self, duration_ms: int | None = None) -> None:
        self.grabbed = False
        self.events.append("open")

    def gripper_close_adaptive(self, stall_threshold: int = 8, max_steps: int = 24) -> int:
        self.grabbed = True
        self.events.append("close")
        # 停在全闭位之前 → 双证据里的「夹爪停滞」成立
        return 338

    def home(self, duration_ms: int = 1500) -> bool:
        self.events.append("home")
        self.moves.append((0.0, 0.17, 0.30))
        self.move_pitches.append(-65.0)
        return True

    def servo_telemetry(self) -> dict[str, dict[str, Any]]:
        return {}


class FakeDetector:
    """只要还没夹住就报一个塑料袋；夹住后目标"消失"（供双证据验证）。"""

    def __init__(self, io: FakeIo) -> None:
        self.io = io
        self.target_class = "plastic_bag"

    def info(self) -> dict[str, Any]:
        return {"ready": True, "backend": "fake", "target_class": self.target_class}

    def targets(self, frame: Any) -> list[FakeDetection]:
        if self.io.grabbed:
            return []
        return [FakeDetection((320.0, 240.0))]

    def infer(self, frame: Any) -> list[FakeDetection]:
        return self.targets(frame)


def make_cfg(place: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        # 与 config.yaml 同口径：脉宽越大越闭
        "arm": {"gripper_open": 200, "gripper_close": 450,
                "home": {"pitch": -65, "pitch_range": [-180, 0]}},
        "agent": {
            "frame_period_ms": 1,          # 视觉伺服周期压到 1ms，测试快
            "max_retry": 1,
            "lock_min_hits": 1,
            "work_radius": 0.25,
            "approach_height": 0.08,
            "grasp_pitch": -175,
            "grip_stall_threshold": 8,
            "place": place if place is not None else {
                "coord": list(HOPPER), "settle_s": 0.0,
                "pitch": -130, "approach_z": 0.12, "drop_z": 0.02, "retreat_z": 0.20,
            },
        },
    }


def make_loop(cfg: dict[str, Any], *, hopper: tuple[float, float] | None = HOPPER,
              source: str = "taught") -> tuple[GraspLoop, FakeIo]:
    io = FakeIo()
    cal = demo_calibration(plane_z=PLANE_Z)
    cal.place = ({"coord": [hopper[0], hopper[1]], "source": source}
                 if hopper is not None else None)
    loop = GraspLoop(io, FakeDetector(io), cal, cfg)
    return loop, io


# ----------------------------------------------------------------------
# 描述口径
# ----------------------------------------------------------------------
def test_describe_place_prefers_coord_and_flags_empty() -> None:
    assert describe_place(None)["ready"] is False
    assert describe_place({})["kind"] == "none"
    assert describe_place({"coord": [0.1, 0.2]})["kind"] == "coord"
    assert describe_place({"pose": {1: 500}})["kind"] == "pose"
    # coord 优先于 pose（同一份配置里两个都写时不歧义）
    mixed = describe_place({"coord": [0.1, 0.2], "pose": {1: 500}})
    assert mixed["kind"] == "coord"


# ----------------------------------------------------------------------
# ⑥ 放置本身
# ----------------------------------------------------------------------
def test_place_coord_goes_to_hopper_then_retreats_home() -> None:
    cfg = make_cfg()
    loop, io = make_loop(cfg)
    log: list[dict[str, Any]] = []

    assert loop._place(log) is True

    # 动作序列：槽口上方(绝对 z=0.12) → 槽内(z=0.02) → 松爪 → 抬离(z=0.20) → 回待命位
    assert io.moves[0] == pytest.approx((HOPPER[0], HOPPER[1], 0.12))
    assert io.moves[1] == pytest.approx((HOPPER[0], HOPPER[1], 0.02))
    assert io.events.index("open") > io.events.index("move")
    assert io.moves[2] == pytest.approx((HOPPER[0], HOPPER[1], 0.20))
    assert io.events[-1] == "home"
    # 放置俯仰角用 place.pitch（厂商侧向工位 -130），不是待命位的 -65
    assert io.move_pitches[:3] == pytest.approx([-130.0, -130.0, -130.0])
    assert log[-1]["placed"] is True and log[-1]["place_kind"] == "coord"
    assert log[-1]["place_pitch"] == -130


def test_grasp_uses_top_down_pitch_not_home_pitch() -> None:
    """抓取俯仰角必须是厂商的 -175（竖直朝下），不是待命位的 -65。"""
    cfg = make_cfg()
    loop, io = make_loop(cfg)

    assert loop.run(task_id="tsk_pitch").ok is True
    # 抓取段（在 home 之前）的每一动都用 -175
    pick_pitches = [p for p, m in zip(io.move_pitches, io.moves)
                    if abs(m[2] - 0.30) > 1e-9 and abs(m[2] - 0.20) > 1e-9
                    and abs(m[2] - 0.12) > 1e-9 and abs(m[2] - 0.02) > 1e-9]
    assert pick_pitches and all(p == -175.0 for p in pick_pitches), pick_pitches


def test_place_pose_replays_taught_pulses() -> None:
    cfg = make_cfg()
    pose = {1: 500, 2: 500, 3: 480, 4: 620, 5: 300, 6: 700}
    io = FakeIo()
    cal = demo_calibration(plane_z=PLANE_Z)
    cal.place = {"pose": pose, "source": "taught"}
    loop = GraspLoop(io, FakeDetector(io), cal, cfg)

    assert loop._place([]) is True
    assert io.servo_batches == [sorted(pose.items())]        # 回放示教位姿
    assert "open" in io.events and io.events[-1] == "home"


def test_config_coord_is_used_when_nothing_taught() -> None:
    """标定文件里没有放置点、但 config 填了 coord → 走 config 兜底。"""
    cfg = make_cfg({"coord": [0.12, -0.03], "settle_s": 0.0})
    loop, io = make_loop(cfg, hopper=None)

    assert loop.place_info()["source"] == "config"
    assert loop._place([]) is True
    assert io.moves[0][:2] == pytest.approx((0.12, -0.03))


def test_place_refuses_without_any_place_point() -> None:
    """没有放置点：放置明确失败，闭环也拒绝开工 —— 不假装放好了。"""
    cfg = make_cfg(None)
    cfg["agent"]["place"] = {}                      # 既无 coord 也无 pose
    loop, io = make_loop(cfg, hopper=None)
    log: list[dict[str, Any]] = []

    assert loop._place(log) is False
    assert log[-1] == {"placed": False, "reason": "no_place_point"}
    assert "open" not in io.events                  # 原地也不松爪，等人工介入

    outcome = loop.run(task_id="tsk_no_place")
    assert outcome.ok is False and outcome.placed is False
    assert "料槽放置点" in outcome.message


# ----------------------------------------------------------------------
# 夹爪方向（按厂商源码核对：脉宽越大越闭）
# ----------------------------------------------------------------------
def test_gripper_closes_toward_larger_pulse_and_stops_on_stall() -> None:
    """开 200 / 闭 450 时，自适应闭合必须**加**着走，并在停滞处停下。"""
    from grasp.arm_io import ArmIo

    cfg = {"arm": {"gripper_open": 200, "gripper_close": 450, "gripper_step": 20,
                   "gripper_step_wait": 0.0,
                   "servo_topic": "/x", "servo_states_topic": "/y"}}
    io = ArmIo(cfg, mock=False)
    sent: list[int] = []
    io.set_servo = lambda sid, pos, duration_ms=None: sent.append(int(pos))  # type: ignore[method-assign]
    # 模拟「夹到袋子」：脉宽到 300 之后回读不再变化（真机就是靠这个停滞判据）
    io.servo_telemetry = lambda: {"1": {"position": min(sent[-1], 300) if sent else 200}}  # type: ignore[method-assign]

    pulse = io.gripper_close_adaptive(stall_threshold=8)

    assert sent[0] == 220, f"第一步方向错了：{sent[:3]}"
    assert all(b > a for a, b in zip(sent, sent[1:])), f"没有一直朝全闭位走：{sent}"
    assert 280 <= pulse < 450, f"应停在半途（夹到东西），实际 {pulse}"


def test_verify_stall_rule_is_direction_agnostic() -> None:
    cfg = make_cfg()
    loop, io = make_loop(cfg)
    io.grabbed = True                      # 目标已从原位消失
    gone, stalled = loop._verify(FakeDetection((320.0, 240.0)), 338, [])
    assert gone is True and stalled is True     # 停在半途 = 夹到了
    assert loop._verify(FakeDetection((320.0, 240.0)), 450, [])[1] is False  # 闭到底 = 空夹


# ----------------------------------------------------------------------
# 端到端：派单 → 识别 → 夹取 → 入槽
# ----------------------------------------------------------------------
def test_full_loop_picks_static_bag_and_drops_it_into_hopper() -> None:
    cfg = make_cfg()
    loop, io = make_loop(cfg)

    outcome = loop.run(task_id="tsk_demo_0001")

    assert outcome.ok is True, outcome.message
    assert outcome.placed is True
    assert outcome.target_gone and outcome.grip_stalled      # 双证据都成立
    assert "放入料槽" in outcome.message
    # 抓到过、也真的把袋子送到了料槽坐标上方（不是原地丢）
    assert "close" in io.events
    reached = [m for m in io.moves if abs(m[0] - HOPPER[0]) < 1e-9 and abs(m[1] - HOPPER[1]) < 1e-9]
    assert reached, f"没走到料槽坐标，实际动作：{io.moves}"
    assert io.events[-1] == "home"
