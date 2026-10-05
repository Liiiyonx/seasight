"""通过厂商 ROS 节点读舵机状态（只读，不驱动）。

背景：`check_arm_hardware.py` 发现两个串口都被占：
  · /dev/ttyAMA0 —— 我自己的探针（已清理）
  · /dev/ttyS0    —— 厂商 `hiwonder_servo_manager`（ROS 节点，PID 8534）

★ 关键推论：厂商节点能长期持有 ttyS0，说明它至少成功**打开**了串口。
  那么问题可能不是"硬件没接"，而是：
    a) 舵机在 ttyAMA0 那条通道上（而ttyS0 只是控制信号，两者不同）
    b) 厂商节点也没读到舵机，只是把串口 open 了而已
  用 rosservice/rostopic 问它最快能分辨。

只读操作：不调任何 set_*/动作接口。
"""
from __future__ import annotations

import sys

import paramiko

CMDS = [
    (
        '舵机 ROS 节点提供哪些服务/话题',
        'source /opt/ros/melodic/setup.bash 2>/dev/null; '
        'rosservice list 2>/dev/null | grep -iE "servo|arm|hiwonder|gripper" '
        '| head -10; echo "--- topics ---"; '
        'rostopic list 2>/dev/null | grep -iE "servo|joint|arm|gripper" '
        '| head -10',
    ),
    (
        '直接问厂商节点舵机状态（若有相关 service）',
        'source /opt/ros/melodic/setup.bash 2>/dev/null; '
        'for s in /hiwonder_servo_manager/read_positions '
        '/hiwonder_servo_manager/get_positions '
        '/hiwonder_servo/read_servo_positions; do '
        'echo "--- $s ---"; '
        'timeout 6 rosservice call $s "{}" 2>&1 | head -4; done',
    ),
    (
        '厂商节点的日志（看它有没有成功读到舵机）',
        'ls -t ~/.ros/log/*/hiwonder_servo_manager*.log 2>/dev/null '
        '| head -1 | xargs -r tail -20 | grep -iE "servo|error|fail|open|position" '
        '| head -10',
    ),
    (
        '扩展板电压（厂商工具可读，能证明扩展板是否上电）',
        'timeout 20 python3 - <<PYEOF 2>&1 | tail -6\n'
        'import sys\n'
        'sys.path.insert(0, "/home/ubuntu")\n'
        'try:\n'
        '    import ros_robot_controller_sdk as rrc\n'
        '    b = rrc.Board(device="/dev/ttyS0", baudrate=115200, timeout=2.0)\n'
        '    b.enable_reception()\n'
        '    import time; time.sleep(0.5)\n'
        '    print("VINS:", b.bus_servo_read_vin(0))\n'
        'except Exception as e:\n'
        '    print("FAIL:", type(e).__name__, str(e)[:60])\n'
        'PYEOF',
    ),
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
        for label, cmd in CMDS:
            print('=== %s ===' % label)
            try:
                _, so, se = cli.exec_command(cmd, timeout=30)
                so.channel.settimeout(30)
                out = so.read().decode('utf-8', 'replace').strip()
                print(out or '(空)')
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
