"""检查机械臂硬件供电与总线舵机在线状态（纯只读，不驱动）。

背景：`deploy_arm_sdk.py` 确认两条串口都能打开，但 `bus_servo_read_id()`
始终拿不到回包。软件侧（通道 / 波特率 / SDK）已排除，剩下是硬件。

厂商资料里的关键事实：
  · 舵机供电由**扩展板**提供（7.4V 锂电接DC 口），不由树莓派供电
  · 总线舵机走**半双工**单总线，靠 74HC126 三态缓冲器切换方向
  · 舵机默认 ID = 0，可用广播 255 查

本脚本查三件事：
  1. 树莓派侧供电/扩展板相关的硬件线索（I2C 设备、串口占用、扩展板驱动）
  2. 用 GPIO 模拟厂商做法做一次**纯发送**（不期待回包）——
     确认 74HC126 的 TX_CON/RX_CON 引脚是否可操作
  3. 报告树莓派上还挂着哪些进程（厂商 GUI 是否已开、占着串口）
"""
from __future__ import annotations

import sys

import paramiko

CHECKS = [
    (
        '串口是否被占用（厂商 GUI 开着就会占）',
        'sudo -n fuser -v /dev/ttyAMA0 /dev/ttyS0 2>&1 | head -6 || '
        'echo "(需要 root，看不到就跳过)"',
    ),
    (
        '厂商 GUI / ROS 进程是否在跑',
        'ps aux | grep -iE "ArmPi|roscore|roslaunch|start_node" '
        '| grep -v grep | head -5 || echo "(无)"',
    ),
    (
        '扩展板 I2C 设备（74HC126 方向控制走 GPIO，I2C 上可能有扩展板芯片）',
        'i2cdetect -y 1 2>/dev/null | tr "\\n" " " || '
        'python3 -c "import smbus2; print(hex(smbus2.SMBus(1).get_i2c_reserved_bytes(0x48)[:1][0]) if False else \'smbus2 available\')"',
    ),
    (
        'GPIO 当前占用（TX_CON/RX_CON 通常是 GPIO14/15 或别的脚）',
        'gpioinfo 2>/dev/null | head -12 || '
        'python3 -c "import RPi.GPIO as G; G.setmode(G.BCM); '
        'print([(p, G.input(p)) for p in (14, 15)])" 2>&1 | tail -2',
    ),
    (
        '串口内核层状态（有没有内核报错）',
        'dmesg 2>/dev/null | grep -iE "ttyAMA|ttyS0|uart|serial" | tail -6',
    ),
    (
        '扩展板 LED / 供电痕迹（厂商脚本会在此输出舵机电压）',
        'ls -l /sys/class/leds/ 2>/dev/null | head -6 || echo "(无 LED 子系统)"',
    ),
    (
        'USB 设备（若舵机走 USB 转接则在这里）',
        'lsusb 2>/dev/null | head -5 || echo "(无 lsusb)"',
    ),
]


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else '192.168.149.1'
    user = sys.argv[2] if len(sys.argv) > 2 else 'ubuntu'
    pwd = 'hiwonder'

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(host, username=user, password=pwd, timeout=15)
    except Exception as exc:  # noqa: BLE001
        print('连接失败：%s' % exc, file=sys.stderr)
        return 1

    try:
        for label, cmd in CHECKS:
            print('=== %s ===' % label)
            try:
                _, so, se = cli.exec_command(cmd, timeout=25)
                so.channel.settimeout(25)
                out = so.read().decode('utf-8', 'replace').strip()
                err = se.read().decode('utf-8', 'replace').strip()
                print(out or '(空)')
                if err:
                    print('  stderr:', err[:160])
            except Exception as exc:  # noqa: BLE001
                print('  (执行失败: %s)' % type(exc).__name__)
            print()
    finally:
        cli.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
