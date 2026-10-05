"""RosArmDriver 的回归测试（不需要真机/ROS）。

覆盖：
  1. 惰性注册 —— import 才进注册表，且必须进**同一份**注册表
     （踩过的坑：ros_driver 用 `from drivers import` 时会注册到另一份
      模块对象，导致配置选 ros_arm_control 报 KeyError）
  2. 无 ROS 环境下必须优雅降级，不能抛异常
  3. /joint_states 读数 → ArmStatus.servos 的转换（键类型与串口路径一致）
  4. 口径：target 是 GPS 不是关节角
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "edge"))
sys.path.insert(0, str(ROOT / "edge" / "arm_bridge"))

from arm_bridge.drivers import (  # noqa: E402
    ARM_DRIVER_REGISTRY,
    DeviceMode,
    Position,
)
import arm_bridge.ros_driver as ros_driver  # noqa: E402


# ---------- ① 注册表 ----------


def test_registers_under_expected_name() -> None:
    assert "ros_arm_control" in ARM_DRIVER_REGISTRY
    assert ros_driver.RosArmDriver is ARM_DRIVER_REGISTRY["ros_arm_control"]


def test_registered_into_the_same_registry_object() -> None:
    """必须注册进 bridge 用的那一份，不能是同名但不同身份的模块。

    这是真实踩过的坑：`from drivers import` 与`from arm_bridge.drivers
    import` 会产生两份 ARM_DRIVER_REGISTRY，配置选 ros_arm_control 时
    报 KeyError。
    ★ 不能用 isinstance 判断 —— ArmDriver 是 Protocol 但没加
      @runtime_checkable，isinstance 会抛 TypeError。改为逐方法核对。
    """
    inst = ARM_DRIVER_REGISTRY["ros_arm_control"]()
    for name in ("status", "pick", "pause", "resume",
                 "return_home", "emergency_stop"):
        assert callable(getattr(inst, name, None)), name


def test_other_drivers_still_present() -> None:
    """新增驱动不能挤掉原有的三个。"""
    for name in ("simulated", "http", "hiwonder_bus_servo"):
        assert name in ARM_DRIVER_REGISTRY, name


# ---------- ② 无 ROS 时降级 ----------


def test_status_degrades_without_ros() -> None:
    d = ros_driver.RosArmDriver()
    st = d.status()          # 不应抛
    assert st.servos == {}
    assert st.mode == DeviceMode.IDLE


def test_pick_without_ros_raises_readable_error() -> None:
    """真要用时必须有可执行的错误信息，不能是 ImportError 堆栈。"""
    d = ros_driver.RosArmDriver()
    try:
        d.pick("T-1", Position(119.65, 26.38), 3)
    except RuntimeError as exc:
        assert "rospy" in str(exc) or "ROS" in str(exc), str(exc)
    except Exception as exc:  # noqa: BLE001
        raise AssertionError("应为 RuntimeError，实际 %r" % exc) from exc
    # 若本机恰好装了 rospy则会走真实分支，这里不断言


def test_emergency_stop_sets_mode_without_ros() -> None:
    d = ros_driver.RosArmDriver()
    d.emergency_stop()
    assert d.status().mode == DeviceMode.E_STOP


# ---------- ③ joint_states 转换 ----------


def test_joint_snapshot_maps_to_servo_dict() -> None:
    """键必须是舵机序号（字符串），与 HiwonderBusServo 路径的键类型一致 ——
    页面的展示逻辑不该为两种驱动写两套。"""
    snap = ros_driver._JointSnapshot(
        names=["joint1", "joint2", "joint3", "joint4", "joint5", "r_joint"],
        positions=[0.0, 0.5236, -1.3614, -1.7593, 0.0, -1.7802],
        velocities=[0.0] * 6,
        efforts=[0.0] * 6,
    )
    out = snap.as_servo_map()
    assert set(out) == {"1", "2", "3", "4", "5", "6"}
    assert out["2"]["position"] == 0.5236
    assert out["2"]["joint"] == "joint2"
    # 度数换算正确
    assert abs(out["2"]["position_deg"] - 30.0) < 0.01
    assert abs(out["4"]["position_deg"] - (-100.8)) < 0.01


def test_snapshot_handles_missing_arrays() -> None:
    """有些消息里 velocity/effort 是空数组，不能 IndexError。"""
    snap = ros_driver._JointSnapshot(
        names=["joint1"], positions=[0.5], velocities=[], efforts=[]
    )
    out = snap.as_servo_map()
    assert out["1"]["position"] == 0.5
    assert "velocity" not in out["1"]


def test_snapshot_with_fewer_joints_than_names() -> None:
    """positions 比 names 短时不能越界。"""
    snap = ros_driver._JointSnapshot(
        names=["joint1", "joint2", "joint3"], positions=[0.1, 0.2]
    )
    out = snap.as_servo_map()
    assert len(out) == 2
    assert out["2"]["joint"] == "joint2"


# ---------- ④ 默认值与口径 ----------


def test_default_home_matches_vendor_calibration() -> None:
    """默认 home 位应与实测 /joint_states 的厂商标定值一致。

    那组值全是整数度（30 / 78 / 100.8 / 102），是厂商标定过的 home 位。
    """
    degs = [round(math.degrees(x), 1) for x in ros_driver.DEFAULT_HOME_RAD]
    assert degs == [0.0, 30.0, -78.0, -100.8, 0.0, -102.0]


def test_joint_names_cover_six_dof() -> None:
    assert len(ros_driver.DEFAULT_JOINT_NAMES) == 6
    assert "r_joint" in ros_driver.DEFAULT_JOINT_NAMES


def test_pick_treats_target_as_gps_not_joint() -> None:
    """★ 口径守护：target 是 GPS 目标点，不能当关节角用。

    逆运动学在厂商 ROS 栈里（armpi_fpv_kinematics），我们不解。
    这个测试存在的意义是：若将来有人把 target 直接当关节角下发，
    单位会错得离谱（GPS 是 119.65，关节角是弧度）。
    """
    d = ros_driver.RosArmDriver()
    lng, lat = 119.6521, 26.3864
    assert abs(lng) > 90, "这是经度，不是关节角"
    assert abs(lat) <= 90
    # 驱动不应把 target 存成关节角
    assert not hasattr(d, "target_joint_rad")
    assert d.home.lng != lng or d.home.lat != lat


# ---------------------------------------------------------------------------
# 限位钳制：演示安全护栏
# ---------------------------------------------------------------------------

def test_clamp_keeps_in_range_values_untouched() -> None:
    """范围内的值原样保留，不能被钳制改小。"""
    names = list(ros_driver.DEFAULT_JOINT_NAMES)
    positions = list(ros_driver.DEFAULT_HOME_RAD)
    clamped, warnings = ros_driver.clamp_joint_angles(names, positions)
    assert clamped == pytest.approx(positions)
    assert warnings == []


def test_clamp_catches_out_of_range_value() -> None:
    """★ 配置写错时必须被拦住并暴露出来，而不是照发。"""
    names = list(ros_driver.DEFAULT_JOINT_NAMES)
    # joint2 限位 ±90°=±1.57，这里给 3.0（≈172°）——远超限位
    bad = [0.0, 3.0, 0.0, 0.0, 0.0, -1.7802]
    clamped, warnings = ros_driver.clamp_joint_angles(names, bad)
    assert len(warnings) == 1, "超限必须产生告警，不能静默"
    assert "joint2" in warnings[0]
    assert clamped[1] == pytest.approx(1.57)


def test_clamp_clamps_gripper_to_safe_range() -> None:
    """机械爪 r_joint 上限 +1.57（厂商 URDF），超限必须被拦。"""
    names = list(ros_driver.DEFAULT_JOINT_NAMES)
    bad = [0.0, 0.0, 0.0, 0.0, 0.0, 2.5]  # 超上限
    clamped, warnings = ros_driver.clamp_joint_angles(names, bad)
    assert clamped[5] == pytest.approx(1.57)
    assert any("r_joint" in w for w in warnings)


def test_clamp_respects_custom_limits() -> None:
    """配置可覆盖限位表 —— 实测拿到 0x32 真值后要能替换。"""
    names = ["joint1"]
    custom = {"joint1": (-0.5, 0.5)}
    clamped, warnings = ros_driver.clamp_joint_angles(
        names, [1.2], custom
    )
    assert clamped[0] == pytest.approx(0.5)
    assert warnings


def test_clamp_handles_length_mismatch_safely() -> None:
    """names 比 positions 短时不能崩（防配置写错长度）。"""
    clamped, warnings = ros_driver.clamp_joint_angles(
        ["joint1"], [0.1, 0.2, 0.3]
    )
    assert len(clamped) == 3
    # 多出来的关节名回退到 jointN
    assert any("joint2" in w or "joint3" in w for w in warnings) or not warnings


def test_driver_uses_configured_joint_limits() -> None:
    """构造函数传入的限位必须真的生效，不是被忽略。"""
    d = ros_driver.RosArmDriver(joint_limits={"joint2": (-0.1, 0.1)})
    assert d.joint_limits["joint2"] == (-0.1, 0.1)
    clamped, _ = ros_driver.clamp_joint_angles(
        ["joint2"], [5.0], d.joint_limits
    )
    assert clamped[0] == pytest.approx(0.1)


def test_default_limits_match_vendor_urdf() -> None:
    """★ 限位表必须与厂商 URDF 实测值一致（2026-10-05 抓取）。

    这张表是**唯一的**位置限位来源 —— 厂商 MoveIt 的 joint_limits.yaml
    里只有 max_velocity，没有位置限位。若将来有人"优化"成更宽的值，
    这个测试会拦住。
    """
    expected = {
        "joint1": (-2.09, 2.09),
        "joint2": (-1.57, 1.57),
        "joint3": (-2.09, 2.09),
        "joint4": (-2.09, 2.09),
        "joint5": (-2.09, 2.09),
        # 下限取实测出厂 home 位（-1.7802），非 URDF 的 -1.57
        "r_joint": (-1.7802, 1.57),
    }
    for name, bounds in expected.items():
        assert ros_driver.DEFAULT_JOINT_LIMITS[name] == pytest.approx(bounds), (
            "%s 限位与厂商 URDF 不符" % name
        )
    # 每个默认关节都必须有限位定义，漏一个就是静默放行
    for name in ros_driver.DEFAULT_JOINT_NAMES:
        assert name in ros_driver.DEFAULT_JOINT_LIMITS, "%s 缺限位定义" % name


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
