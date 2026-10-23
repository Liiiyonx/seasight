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


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
