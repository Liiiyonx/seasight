"""检查树莓派上 RosArmDriver 所需的依赖。

RosArmDriver 用到三个 ROS 包，必须都在才能跑：
  rospy                —— 节点、订阅、action client
  sensor_msgs/JointState —— 读 /joint_states
  control_msgs         —— FollowJointTrajectoryGoal、GripperCommand
  trajectory_msgs      —— JointTrajectoryPoint

另外要确认 bridge 自身的依赖（paho-mqtt 等）是否齐。
"""
from __future__ import annotations

import sys

import paramiko

CHECKS = [
    ('ROS Python 包（RosArmDriver 必需）',
     'source /opt/ros/melodic/setup.bash 2>/dev/null; '
     'for m in rospy sensor_msgs.msg control_msgs.msg trajectory_msgs.msg; do '
     'python3 -c "import $m" 2>/dev/null && echo "  OK   $m" '
     '|| echo "  MISS $m"; done'),
    ('bridge 自身依赖',
     'for m in yaml paho.mqtt.client; do '
     'python3 -c "import $m" 2>/dev/null && echo "  OK   $m" '
     '|| echo "  MISS $m"; done'),
    ('action server 是否就绪（RosArmDriver 的前提）',
     'source /opt/ros/melodic/setup.bash 2>/dev/null; '
     'rosservice list 2>/dev/null | grep -c "follow_joint_trajectory" '
     '|| echo 0; echo "(应为 2：arm_controller + gripper_controller)"'),
    ('joint_states 是否在发布',
     'source /opt/ros/melodic/setup.bash 2>/dev/null; '
     'timeout 6 rostopic hz /joint_states 2>&1 | head -3'),
    ('broker 状态（bridge 要连它）',
     'systemctl is-active mosquitto 2>/dev/null || echo "inactive"; '
     'ss -tln 2>/dev/null | grep -E "1883" || echo "  1883 未监听"'),
    ('bridge 目标目录',
     'ls -d ~/seasight_bridge ~/arm_bridge /opt/seasight/bridge 2>/dev/null '
     '|| echo "  尚未部署 bridge"'),
    ('树莓派磁盘余量（部署需要空间）',
     'df -h ~ | tail -1'),
]


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else '192.168.149.1'
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(host, username='ubuntu', password='hiwonder', timeout=15)
    except Exception as exc:  # noqa: BLE001
        print('连接失败：%s' % exc, file=sys.stderr)
        return 1
    try:
        for label, cmd in CHECKS:
            print('=== %s ===' % label)
            try:
                _, so, se = cli.exec_command(cmd, timeout=30)
                so.channel.settimeout(30)
                out = so.read().decode('utf-8', 'replace').strip()
                print(out or '(空)')
                err = se.read().decode('utf-8', 'replace').strip()
                if err:
                    print('  stderr:', err[:160])
            except Exception as exc:  # noqa: BLE001
                print('  (失败: %s)' % type(exc).__name__)
            print()
    finally:
        cli.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
