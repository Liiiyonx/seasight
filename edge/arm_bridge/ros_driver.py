"""通过厂商 ROS 控制栈驱动机械臂（ros_control action）。

## 为什么需要这个驱动

2026-10-05 实测发现：厂商的 ``hiwonder_servo_manager`` 节点**已经持有
``/dev/ttyS0``**，并且 ``/joint_states`` 在正常发布 6 个关节的实时角度。
也就是说机械臂已被厂商 ROS 栈接管，我们的驱动若再去直连串口：

* 物理上抢不到总线（半双工，一发一收必须独占）
* 逻辑上也不该抢 —— 绕过厂商程序会失去它的逆运动学与动作组

所以这里实现的是**同一个 `ArmDriver` 协议的另一种执行端**：
平台与桥接层一行不改，只把 ``driver.backend`` 换成 ``ros_arm_control``。

这正是"执行端可替换层"最实在的例证 —— 驱动的实现从"直连串口"
换成"ROS action"，平台侧的派单逻辑、状态机、证据链完全不动。

## 与 HiwonderBusServoArmDriver 的差别

======================  =========================  ==========================
                        HiwonderBusServo（串口）    RosArmDriver（ROS action）
======================  =========================  ==========================
控制方式                 示教位置序列直接下发        关节角轨迹 /末端坐标
位置语义                 脉宽 0–1000（无量纲）      弧度（有物理意义）
机械爪                   序列里的一组舵机           /gripper_controller 独立
遥测                     读电压/温度/脉宽           读 /joint_states 角度
占用关系                 与厂商程序互斥★            与厂商程序**共存**
======================  =========================  ==========================

★ 串口那条路必须先停掉厂商节点才能用，ROS 这条路不用。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

# ★ 用相对导入：bridge.py 以 `from arm_bridge.drivers import ...` 方式加载，
#   若这里写 `from drivers import`，Python 会把它当成**另一个**顶层模块，
#   于是注册进的是两份不同的 ARM_DRIVER_REGISTRY，配置选 ros_arm_control
#   会报 KeyError。同理 device_sim 也要用完整包路径。
from .drivers import (
    ArmDriver,
    ArmStatus,
    DeviceMode,
    PickResult,
    Position,
    register_arm_driver,
)

# 关节名 → 我们的 servo_id 映射。厂商 URDF 用 joint1..5 + r_joint。
# ★ 这是**读回**用的（遥测展示）；下发轨迹时顺序必须与 URDF 一致，
#   所以两者共用这一个列表，避免两处漂移。
DEFAULT_JOINT_NAMES = [
    "joint1",
    "joint2",
    "joint3",
    "joint4",
    "joint5",
    "r_joint",
]

# home 位（弧度）—— 与实测 /joint_states 的厂商标定值一致。
DEFAULT_HOME_RAD = [0.0, 0.5236, -1.3614, -1.7593, 0.0, -1.7802]

# 抓取姿态（弧度）。★ 这是**默认值**，真机首跑前应当示教确认 ——
#   直接用 home位当抓取位会抓空。配置里可覆盖。
DEFAULT_PICK_RAD = [0.0, 0.5236, -1.3614, -1.7593, 0.0, -1.7802]


@dataclass
class _JointSnapshot:
    """一次 /joint_states 读数。"""

    names: list[str] = field(default_factory=list)
    positions: list[float] = field(default_factory=list)
    velocities: list[float] = field(default_factory=list)
    efforts: list[float] = field(default_factory=list)
    stamp: float = 0.0

    def as_servo_map(self) -> dict[str, Any]:
        """转成 ArmStatus.servos 用的 {id: {...}} 结构。

        键用**舵机序号**（1..n）而不是 joint 名，与 HiwonderBusServo 那条
        路径的键类型保持一致 —— 页面的展示逻辑不必为两种驱动写两套。
        """
        out: dict[str, Any] = {}
        for i, pos in enumerate(self.positions, start=1):
            item: dict[str, Any] = {
                # 弧度 + 度，度更直观；position 保留原值便于核对
                "position": round(pos, 4),
                "position_deg": round(math.degrees(pos), 2),
                "joint": self.names[i - 1] if i - 1 < len(self.names) else "?",
            }
            if i - 1 < len(self.velocities):
                item["velocity"] = round(self.velocities[i - 1], 4)
            if i - 1 < len(self.efforts):
                item["effort"] = round(self.efforts[i - 1], 3)
            out[str(i)] = item
        return out


class RosArmDriver:
    """用 ros_control action 驱动厂商机械臂，实现同一份 ArmDriver 协议。

    ROS 依赖是**惰性导入**的：没装 rospy 的机器（比如开发机、CI）仍能
    import 本模块并跑其余驱动的测试，只是不注册 ros_arm_control——
    这与 HiwonderBusServoArmDriver 对 pyserial /厂商 SDK 的处理一致。
    """

    def __init__(
        self,
        arm_controller: str = "arm_controller",
        gripper_controller: str = "gripper_controller",
        joint_states_topic: str = "/joint_states",
        joint_names: list[str] | None = None,
        home_rad: list[float] | None = None,
        pick_rad: list[float] | None = None,
        move_timeout: float = 15.0,
        action_done_timeout: float = 20.0,
        collected_weight: float = 0.0,
        review_result: str = "recheck",
        evidence_url: str | None = None,
        battery: int = 100,
        bins: dict[str, float] | None = None,
        home: dict[str, float] | None = None,
    ) -> None:
        self.arm_controller = str(arm_controller)
        self.gripper_controller = str(gripper_controller)
        self.joint_states_topic = str(joint_states_topic)
        self.joint_names = list(joint_names or DEFAULT_JOINT_NAMES)
        self.home_rad = [float(x) for x in (home_rad or DEFAULT_HOME_RAD)]
        self.pick_rad = [float(x) for x in (pick_rad or DEFAULT_PICK_RAD)]
        self.move_timeout = float(move_timeout)
        self.action_done_timeout = float(action_done_timeout)
        self.collected_weight = float(collected_weight)
        self.review_result = str(review_result)
        self.evidence_url = evidence_url
        self.battery = int(battery)
        self.bins: dict[str, float] = dict(
            bins or {"foam": 0.0, "plastic": 0.0, "mixed": 0.0}
        )
        self.home: Position = Position(
            float((home or {}).get("lng", 119.6540)),
            float((home or {}).get("lat", 26.3870)),
        )

        self._mode = DeviceMode.IDLE
        self._node: Any = None
        self._last: _JointSnapshot | None = None

    # ---------- ROS 惰性接入 ----------

    def _ensure_node(self) -> Any:
        """建rospy 节点并订阅 /joint_states（只订阅，不发任何指令）。"""
        if self._node is not None:
            return self._node
        try:
            import rospy
            from sensor_msgs.msg import JointState
        except ImportError as exc:  # pragma: no cover - 取决于运行环境
            raise RuntimeError(
                "ROS Python (rospy + sensor_msgs) is unavailable. Install "
                "ros-<distro>-rospy on the arm host, or pick another "
                "driver.backend (e.g. simulated / http)."
            ) from exc
        rospy.init_node(
            "seasight_ros_arm_driver", anonymous=True, disable_signals=True
        )
        self._node = rospy
        self._JointState = JointState
        self._node.Subscriber(
            self.joint_states_topic, JointState, self._on_joint_states, queue_size=1
        )
        # 给订阅一点时间拿到第一帧，否则首次 status() 会是空
        deadline = time.time() + 3.0
        while time.time() < deadline and self._last is None:
            time.sleep(0.1)
        return self._node

    def _on_joint_states(self, msg: Any) -> None:  # pragma: no cover - 需 ROS
        self._last = _JointSnapshot(
            names=list(msg.name),
            positions=[float(p) for p in msg.position],
            velocities=[float(v) for v in msg.velocity],
            efforts=[float(e) for e in msg.effort],
            stamp=msg.header.stamp.to_sec() if msg.header else 0.0,
        )

    # ---------- ArmDriver 协议 ----------

    def status(self) -> ArmStatus:
        """读当前关节角作为遥测。

        读不到时返回上次快照（可能为空 dict）而不抛异常 —— 遥测失败不该
        让整条平台链路挂掉，这一点与串口驱动的处理一致。
        """
        servos: dict[str, Any] = {}
        try:
            self._ensure_node()
            if self._last is not None:
                servos = self._last.as_servo_map()
        except Exception:  # noqa: BLE001
            pass
        return ArmStatus(
            mode=self._mode,
            battery=self.battery,
            bins=dict(self.bins),
            location=self.home,
            servos=servos,
        )

    def pick(self, task_id: str, target: Position, priority: int) -> PickResult:
        """执行一次抓取。

        ★ 重要口径：``target`` 是平台的 **GPS 目标点**，**不是关节角**。
          末端坐标 → 关节角的逆运动学由厂商 ROS 栈负责
          （``armpi_fpv_kinematics``），我们不自己解 —— 解了也对不上
          厂商的标定。逆运动学源码未取回前，不要宣称"坐标级抓取精度"。
        """
        rospy = self._ensure_node()
        self._mode = DeviceMode.COLLECTING
        try:
            self._send_trajectory(self.pick_rad)
            self._close_gripper(rospy)
            self._open_gripper(rospy)
        except Exception as exc:  # noqa: BLE001
            self._mode = DeviceMode.IDLE
            raise RuntimeError("ROS pick failed: %s" % exc) from exc
        self._mode = DeviceMode.IDLE
        self.bins["foam"] = min(
            1.0, self.bins["foam"] + self.collected_weight
        )
        return PickResult(
            ok=True,
            collected_weight=self.collected_weight,
            review_result=self.review_result,
            evidence_url=self.evidence_url,
            bins_after=dict(self.bins),
        )

    def _send_trajectory(self, positions: list[float]) -> None:  # pragma: no cover
        """经 follow_joint_trajectory action 下发关节角轨迹。"""
        from control_msgs.msg import FollowJointTrajectoryGoal
        from trajectory_msgs.msg import JointTrajectoryPoint

        rospy = self._node
        goal = FollowJointTrajectoryGoal()
        goal.trajectory.joint_names = list(self.joint_names)
        point = JointTrajectoryPoint()
        point.positions = [float(x) for x in positions]
        point.time_from_start = rospy.Duration(self.move_timeout)
        goal.trajectory.points = [point]

        client = rospy.SimpleActionClient(
            "/%s/follow_joint_trajectory" % self.arm_controller,
            FollowJointTrajectoryGoal,
        )
        if not client.wait_for_server(rospy.Duration(5.0)):
            raise RuntimeError(
                "action server /%s/follow_joint_trajectory not available"
                % self.arm_controller
            )
        client.send_goal(goal)
        if not client.wait_for_result(rospy.Duration(self.action_done_timeout)):
            client.cancel_goal()
            raise RuntimeError("trajectory action timed out")
        state = client.get_state()
        label = {
            3: "succeeded",
            4: "aborted",
            5: "preempted",
        }.get(state, "state=%s" % state)
        if label != "succeeded":
            raise RuntimeError("trajectory %s" % label)

    def _close_gripper(self, rospy: Any) -> None:  # pragma: no cover
        """收拢机械爪（gripper_controller 独立于 arm_controller）。"""
        from control_msgs.msg import GripperCommand

        pub = rospy.Publisher(
            "/%s/command" % self.gripper_controller, GripperCommand, queue_size=1
        )
        for _ in range(10):
            if pub.get_num_connections() > 0:
                break
            rospy.sleep(0.2)
        pub.publish(GripperCommand(position=0.0, max_effort=1.0))
        rospy.sleep(1.0)

    def _open_gripper(self, rospy: Any) -> None:  # pragma: no cover
        from control_msgs.msg import GripperCommand

        pub = rospy.Publisher(
            "/%s/command" % self.gripper_controller, GripperCommand, queue_size=1
        )
        for _ in range(10):
            if pub.get_num_connections() > 0:
                break
            rospy.sleep(0.2)
        pub.publish(GripperCommand(position=1.0, max_effort=1.0))
        rospy.sleep(1.0)

    def pause(self) -> None:
        self._mode = DeviceMode.PAUSED

    def resume(self) -> None:
        self._mode = (
            DeviceMode.NAVIGATING if self._mode == DeviceMode.PAUSED
            else DeviceMode.IDLE
        )

    def return_home(self) -> None:  # pragma: no cover
        try:
            rospy = self._ensure_node()
            self._send_trajectory(self.home_rad)
        except Exception:  # noqa: BLE001
            pass
        self._mode = DeviceMode.IDLE

    def emergency_stop(self) -> None:  # pragma: no cover
        """急停：取消所有正在执行的轨迹，并置 E_STOP。

        取消而不是"发home" —— 急停时最不该做的就是让机械臂继续动。
        """
        try:
            rospy = self._ensure_node()
            from control_msgs.msg import FollowJointTrajectoryGoal

            client = rospy.SimpleActionClient(
                "/%s/follow_joint_trajectory" % self.arm_controller,
                FollowJointTrajectoryGoal,
            )
            if client.wait_for_server(rospy.Duration(2.0)):
                client.cancel_all_goals()
                client.cancel_goal()
        except Exception:  # noqa: BLE001
            pass
        self._mode = DeviceMode.E_STOP


# 惰性注册：只有 import 本模块才会进注册表，避免污染其它驱动的测试环境。
register_arm_driver("ros_arm_control", RosArmDriver)

__all__ = ["RosArmDriver", "DEFAULT_JOINT_NAMES", "DEFAULT_HOME_RAD"]
