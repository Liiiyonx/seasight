"""树莓派（ArmPiFPV）信息采集 —— 决定平台桥接层能否直接驱动真机。

## 为什么需要这个脚本

平台侧 `edge/arm_bridge/drivers.py` 的 `HiwonderBusServoArmDriver`
**假设**真机是「总线舵机 + 厂商 `ros_robot_controller_sdk.py`，走串口」。
但2026-10-05 实测发现树莓派 8080 端口跑的是 **ROS 图像话题栈**
（`/object_sorting`、`/object_tracking` 等）。

★ 两者是不是同一套东西，**目前未经验证**。这个脚本就是去回答它：
如果真机走的是 ROS 服务/话题而不是串口 SDK，那桥接层要改，
不改就派单下去机械臂不会动 —— 而这种失败在演示现场才会暴露。

## 用法

    python scripts/probe_raspberry_pi.py \
        --host 192.168.149.1 --user ubuntu --password hiwonder

密码走命令行参数会被 shell 历史记录，演示机建议改用环境变量：
    set PI_PASSWORD=hiwonder  (Windows)
    export PI_PASSWORD=...    (bash)
    python scripts/probe_raspberry_pi.py --host 192.168.149.1

输出为 Markdown，可直接贴进 docs/competitions/ 下的探测记录。
"""
from __future__ import annotations

import argparse
import io
import sys
from datetime import datetime

try:
    import paramiko
except ImportError:
    print('需要 paramiko：pip install paramiko cryptography', file=sys.stderr)
    sys.exit(2)

# 每条命令后标注它想回答的问题，输出里保留，方便对着结论做决策
PROBES = [
    ('系统与网络',
     'hostname; uname -a; cat /etc/os-release | head -2',
     '系统版本、架构（决定能否直接跑厂商 arm 二进制）'),

    ('★ STA 模式现状（决定演示网络方案）',
     'ls /etc/wpa_supplicant/wpa_supplicant.conf 2>/dev/null && '
     'sed -e "s/psk=.*/psk=***REDACTED***/" /etc/wpa_supplicant/wpa_supplicant.conf || '
     'echo "无 wpa_supplicant.conf（可能仍是 AP 直连模式）"',
     '★ 关键：是否已配 STA。有 network={ssid=...} 才是STA；只有本机热点配置则是 AP'),

    ('网络接口与地址',
     'ip -4 addr show | grep -E "^[0-9]+:|inet " ; echo "--- routes ---"; ip route',
     'wlan0 有没有拿到演示网 IP；有无默认网关（决定能否出网访问平台）'),

    # ★ 这一条被我写错过一次，务必保留全量列举+ test -e 双重判据。
    # 板载 UART 是 ttyAMA0 / ttyS0，**根本没有 ttyUSB* 节点** ——
    # 总线舵机走板载串口、不经 USB 转串口，所以"无 ttyUSB"完全正常。
    # 上一版据此误判成"舵机串口不存在"。
    ('★ 舵机串口（全量列举）',
     'for d in /dev/ttyAMA0 /dev/ttyS0 /dev/serial0 /dev/ttyUSB0 /dev/ttyACM0; do '
     'test -e $d && echo "$d  存在" || echo "$d  不存在"; done; '
     'echo "--- ls -l ---"; ls -l /dev/ttyAMA* /dev/ttyS* /dev/ttyUSB* 2>/dev/null; '
     'echo "--- 内核日志 ---"; dmesg 2>/dev/null | grep -iE "ttyAMA|uart-pl011" | tail -3',
     '★ 关键：板载 UART 是 ttyAMA0/ttyS0，不是 ttyUSB。'
     '用 test -e 判定，别用 ls /dev/ttyUSB*'),

    ('★ 板载UART 的内核设备树',
     'cat /proc/cmdline | tr " " "\n" | grep -E "8250|console" ; '
     'echo "--- boot config ---"; '
     'grep -iE "enable_uart|dtparam.*uart" /boot/firmware/config.txt /boot/config.txt 2>/dev/null | head -4',
     '判断板载 UART 是否被蓝牙占用（Pi4 蓝牙默认占用 ttyAMA0）'),

    ('★ 厂商控制通道（决定我们该用哪个串口）',
     'grep -rnE "serial\\.Serial\\(" ~/ArmPi_PC_Software/*.py 2>/dev/null | head -5; '
     'echo "--- 我们的 SDK 默认 ---"; '
     'find / -name "ros_robot_controller_sdk.py" -not -path "*/proc/*" 2>/dev/null | head -3',
     '★ 最关键：厂商 GUI 与厂商示例用**不同串口/波特率**。'
     'GUI=/dev/ttyS0@115200，示例=/dev/ttyAMA0@1000000。'
     '别和厂商 GUI 同时开（抢串口）'),

    ('★ 厂商预置动作组（来不及示教时的兜底）',
     'ls -1 ~/ArmPi_PC_Software/ActionGroups/ 2>/dev/null | head -8 || echo "无预置动作组"',
     '★ 演示兜底：预置动作组能直接演示"派单→机械臂动作→进度回传"闭环，'
     '动作是真执行的，不违反不伪造口径'),

    ('★ Python 依赖（别重复 pip install）',
     'python3 -c "import serial; print(1)" >/dev/null 2>&1 '
     '&& echo "pyserial OK" || echo "pyserial 缺失"; '
     'python3 -c "import RPi.GPIO" >/dev/null 2>&1 '
     '&& echo "RPi.GPIO OK" || echo "RPi.GPIO 缺失"; '
     'python3 -c "import smbus2" >/dev/null 2>&1 '
     '&& echo "smbus2 OK" || echo "smbus2 缺失"',
     '树莓派出厂已预装 pyserial/RPi.GPIO/smbus2，通常不需再装'),

    ('★ 厂商 SDK 是否在位',
     'find ~ /opt -maxdepth 3 -name "ros_robot_controller_sdk.py" 2>/dev/null; '
     'python3 -c "import ros_robot_controller_sdk; print(\'SDK 可导入\')" 2>&1 | tail -1',
     '★ 关键：SDK 在则示教工具能直接跑；不在则要先从厂商资料补齐'),

    ('★ ROS 栈详情',
     'which rostopic rosnode ros2 2>/dev/null; echo "--- nodes ---"; '
     'rosnode list 2>/dev/null | head -15 || echo "无 rosnode"; echo "--- topics ---"; '
     'rostopic list 2>/dev/null | head -20 || echo "无 rostopic"',
     '★ 关键：判断舵机控制走ros service 还是 topic —— 这决定桥接层要不要改'),

    ('★舵机控制接口（最关键的一问）',
     'rostopic list 2>/dev/null | grep -iE "servo|joint|arm|gripper|move|action" || '
     'rosservice list 2>/dev/null | grep -iE "servo|joint|arm|gripper|move" || '
     'echo "未发现舵机相关 topic/service"',
     '★★ 最关键：真机怎么被指挥？平台桥接层要按这个接口写'),

    ('MQTT（平台闭环的另一半）',
     'systemctl is-active mosquitto 2>/dev/null; ss -tlnp 2>/dev/null | grep -E "1883|8883" || '
     'echo "未监听 MQTT 端口"',
     '★ 关键：树莓派上是否有 broker；平台桥接层要连它'),

    ('NoMachine',
     'dpkg -l 2>/dev/null | grep -i nomachine | awk \'{print $2, $3}\' || '
     'systemctl status nomachine --no-pager 2>/dev/null | head -3',
     '版本（社区版无 Web Player，需记录以便评委问起时解释）'),

    ('桌面环境（决定 NoMachine 里能不能看到 GUI）',
     'echo "DISPLAY/desktop:"; ls /etc/X11 2>/dev/null | head -3; '
     'systemctl status lightdm gdm x11vnc 2>/dev/null | grep -E "Active|Loaded" | head -4; '
     'who 2>/dev/null | head -3',
     '★ 关键：X11 会话是否在跑 —— 没跑则 NoMachine 连上也是黑屏'),

    ('候选 Web 上位机 / 厂商应用',
     'ls ~/Desktop /opt/*/  2>/dev/null | head -25; echo "--- 桌面应用 ---"; '
     'ls /usr/share/applications 2>/dev/null | head -15',
     '最终确认 C 档（原生 Web 上位机）是否存在'),

    ('磁盘与运行时长（评估稳定性）',
     'df -h / | tail -1; uptime',
     '磁盘余量不足会导致录屏/抓图失败'),
]

TIP = """
---

## 采集结果的判读

拿到这份输出后，按下面三条分支决定桥接层要不要改：

**分支 1 —— 有串口 + 有 SDK + 无 ROS 舵机话题**
最理想。现有 `HiwonderBusServoArmDriver` 不用改，直接跑
`tools/teach_hiwonder_sequence.py` 示教即可。

**分支 2 —— 舵机走 ROS service/topic（当前 8080 的迹象指向这个）**
**需要改桥接层**。`HiwonderBusServoArmDriver` 假设的是串口直连，
对 ROS 话题是瞎的。要么新增一个 `RosArmDriver` 实现同一份 ArmDriver
协议（仍满足"可替换层"的口径），要么在 ROS 侧写一个节点把话题转成串口。
★ 这正是"可替换层"的价值现场：换一个执行端，平台一行代码不动。

**分支 3 —— 两者都有但不确定**
先跑示教工具录一段短序列验证真机能动；动不了再按分支 2 走。

无论哪个分支，**首次真机跑一律用 3–5 个姿态的短序列**，
别一上来就长序列 —— 跑偏时物理损害与工作量都是成倍的。
"""


def main():
    ap = argparse.ArgumentParser(description='采集树莓派信息（只读，不改任何配置）')
    ap.add_argument('--host', required=True, help='树莓派 IP')
    ap.add_argument('--user', default='ubuntu')
    ap.add_argument('--password', default=None, help='留空则读环境变量 PI_PASSWORD')
    ap.add_argument('--timeout', type=int, default=20)
    args = ap.parse_args()

    import os
    password = args.password or os.environ.get('PI_PASSWORD', '')
    if not password:
        print('未提供密码：加 --password 或设置环境变量 PI_PASSWORD', file=sys.stderr)
        return 2

    try:
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cli.connect(args.host, username=args.user, password=password,
                    timeout=args.timeout, banner_timeout=args.timeout,
                    auth_timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001
        print('连接失败 %s@%s：%s' % (args.user, args.host, exc), file=sys.stderr)
        print('排查：① 树莓派是否已上电 ② 是否连上了它的 AP 热点 ③ 密码是否正确',
              file=sys.stderr)
        return 1

    out = ['# 树莓派（ArmPiFPV）信息采集记录', '',
           '- 采集时间：%s' % datetime.now().strftime('%Y-%m-%d %H:%M'),
           '- 目标：`%s@%s`' % (args.user, args.host),
           '- 方式：SSH 只读探测，**未修改任何配置**', '', '---', '']

    try:
        for title, cmd, why in PROBES:
            out.append('## %s' % title)
            out.append('')
            out.append('> 为什么问：%s' % why)
            out.append('')
            out.append('```')
            try:
                _, so, se = cli.exec_command(cmd, timeout=args.timeout)
                so.channel.settimeout(args.timeout)
                text = so.read().decode('utf-8', 'replace').strip()
                err = se.read().decode('utf-8', 'replace').strip()
                out.append(text if text else '(无输出)')
                if err:
                    out.append('--- stderr ---')
                    out.append(err)
            except Exception as exc:  # noqa: BLE001
                out.append('(执行失败：%s)' % exc)
            out.append('```')
            out.append('')
    finally:
        cli.close()

    out.append(TIP)
    text = '\n'.join(out)
    print(text)

    # 同时落盘，方便直接引用
    path = 'artifacts/raspberry-pi-probe.md'
    try:
        io.open(path, 'w', encoding='utf-8').write(text)
        print('\n已写入 %s' % path, file=sys.stderr)
    except OSError as exc:
        print('（写文件失败：%s）' % exc, file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
