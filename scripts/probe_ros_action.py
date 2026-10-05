"""核实 ros_control action server 是否真的可用（RosArmDriver 的前提）。

★ 上一轮我用 `rosservice list | grep follow_joint_trajectory` 查，结论是
  "0 个 action server" —— **那是查错了**：action 的 goal/result/feedback
  都是**话题**，不是 service。正确查法是 rostopic list。

这个坑值得记：ros_control 的 action 接口在 rostopic 里能直接看到
（xxx/follow_joint_trajectory/{goal,result,feedback,cancel,status}），
而 rosservice 里只有 controller_manager 的那几个。
"""
from __future__ import annotations

import sys

import paramiko

CMD = r'''source /opt/ros/melodic/setup.bash 2>/dev/null
echo "--- follow_joint_trajectory 相关话题 ---"
rostopic list 2>/dev/null | grep "follow_joint_trajectory" | sort
echo "--- controller_manager 服务 ---"
rosservice list 2>/dev/null | grep controller_manager | sort
echo "--- action 客户端能否看到 server ---"
rostopic info /arm_controller/follow_joint_trajectory/goal 2>/dev/null | head -4
'''


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else '192.168.149.1'
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(host, username='ubuntu', password='hiwonder', timeout=20)
    except Exception as exc:  # noqa: BLE001
        print('连接失败：%s' % exc, file=sys.stderr)
        return 1
    try:
        _, so, se = cli.exec_command(CMD, timeout=30)
        so.channel.settimeout(30)
        print(so.read().decode('utf-8', 'replace').strip())
        err = se.read().decode('utf-8', 'replace').strip()
        if err:
            print('stderr:', err[:200])
    finally:
        cli.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
