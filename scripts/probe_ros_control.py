"""读 ros_control 机械臂控制器的实时状态（纯只读）。

★ 关键发现：厂商 ROS 栈里有标准的 ros_control 接口 ——
  /arm_controller/follow_joint_trajectory/{goal,result,feedback}
  /arm_controller/state
  /gripper_controller/...
  /joint_states （若发布）

  这意味着**机械臂已经能被 ROS 控制了**，不必我们自己走串口 ——
  那条路（直连串口）反而是与厂商节点抢总线。

所以对接方案要改：走ROS action/topic，而不是抢 /dev/ttyS0。

本脚本只订阅与查询，不发任何 goal。
"""
from __future__ import annotations

import sys

import paramiko

SCRIPT = r'''import json
import time

import rospy
from rosgraph_msgs.msg import Clock  # noqa: F401  确保rospy 环境完整
try:
    from sensor_msgs.msg import JointState
except Exception:
    JointState = None

rospy.init_node("seasight_readonly_probe", anonymous=True, disable_signals=True)

out = {"ok": False}

# 1) joint_states：最直接 —— 关节角直接说明舵机是否在动/被读到
if JointState is not None:
    try:
        msg = rospy.wait_for_message("/joint_states", JointState, timeout=8)
        out["joint_names"] = list(msg.name)
        out["positions"] = [round(float(p), 4) for p in msg.position]
        out["velocities"] = [round(float(v), 4) for v in msg.velocity]
        out["efforts"] = [round(float(e), 3) for e in msg.effort]
        out["ok"] = True
    except Exception as exc:
        out["joint_states_err"] = "%s: %s" % (type(exc).__name__, str(exc)[:80])
else:
    out["joint_states_err"] = "sensor_msgs 不可用"

# 2) 控制器列表与状态
try:
    out["controllers"] = {
        c["name"]: c["type"] for c in
        __import__("rospy").get_rostime().to_sec() * [0] or []  # 占位
    }
except Exception:
    pass

print("__JSON__" + json.dumps(out, ensure_ascii=False))
'''

CLI_SCRIPT = r'''source /opt/ros/melodic/setup.bash
source ~/armpi_fpv/devel/setup.bash 2>/dev/null
echo "=== 控制器列表 ==="
rosservice call /controller_manager/list_controllers "{}" 2>&1 | head -20
echo "=== 是否有 joint_states ==="
timeout 6 rostopic echo -n1 /joint_states 2>&1 | head -20
'''


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
        sftp = cli.open_sftp()
        with sftp.open('/home/ubuntu/_ros_probe.py', 'w') as f:
            f.write(SCRIPT)
        sftp.close()

        for label, cmd in [
            ('ROS 控制器与joint_states',
             'source /opt/ros/melodic/setup.bash && '
             'cd ~ && timeout 30 python3 _ros_probe.py 2>&1 | tail -6'),
            ('controller_manager 列表',
             'source /opt/ros/melodic/setup.bash && '
             'timeout 10 rosservice call /controller_manager/list_controllers "{}" '
             '2>&1 | head -18'),
        ]:
            print('=== %s ===' % label)
            try:
                _, so, se = cli.exec_command(cmd, timeout=40)
                so.channel.settimeout(40)
                print(so.read().decode('utf-8', 'replace').strip() or '(空)')
                err = se.read().decode('utf-8', 'replace').strip()
                if err:
                    print('  stderr:', err[:200])
            except Exception as exc:  # noqa: BLE001
                print('  (失败: %s)' % type(exc).__name__)
            print()
    finally:
        cli.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
