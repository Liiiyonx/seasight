#!/usr/bin/env python3
"""机械臂到货一键体检（纯只读，绝不下发任何运动指令）。

背景：机械臂到货、热点已连通。此前 16 项验收清单里第 1 项只记了「SSH 通」，
本脚本把「到货后能自己复现的证据」固化下来，避免现场口述无凭据。

三层探测，从不需要密码到需要密码逐级加深：

  第 1 层 · 免密端口探测   —— ping + 22/11311/4000/8080/1883 端口，只用 TCP 握手
  第 2 层 · SSH 只读体检   —— ROS 运行时、板载串口、串口占用、STA 出网能力
  第 3 层 · 驱动 status()  —— 读 6 关节角，不下发轨迹（等价于 deploy_bridge 的第4步）

用法：
    python scripts/check_arm_onboard.py
    python scripts/check_arm_onboard.py --host 192.168.149.1 --user ubuntu
    python scripts/check_arm_onboard.py --ports-only        # 只跑免密那层

凭据走环境变量 PI_PASSWORD，不落盘、不进仓库：
    set PI_PASSWORD=xxx   (Windows: setx PI_PASSWORD xxx)

安全边界（与 check_arm_hardware.py / deploy_bridge_to_pi.py 一致）：
本脚本只发只读命令。不调 `follow_joint_trajectory`，不写 `pick_rad`，
不驱动任何关节。示教抓取姿态必须由人在机械臂旁手动摆位。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from typing import NamedTuple


class Probe(NamedTuple):
    port: int
    label: str
    why: str


PORTS: tuple[Probe, ...] = (
    Probe(22, "SSH", "远程登录 / 部署与诊断通道"),
    Probe(11311, "ROS Master", "roscore 在跑 → 机械臂控制栈活着"),
    Probe(4000, "NoMachine", "远程桌面，上位机图形操作入口"),
    Probe(8080, "厂商静态页", "ArmPi 厂商 web 面板（注意：静态清单≠运行时证据）"),
    Probe(1883, "mosquitto", "MQTT broker，bridge 上报遥测需要"),
    Probe(8100, "Oceanus MCP", "本项目 MCP 服务部署端口"),
)

# 只读命令。逐条都必须是 read-only。
SSH_CHECKS: tuple[tuple[str, str], ...] = (
    ("系统与运行时间",
     "uname -a; uptime"),
    ("ROS 节点（运行时，非静态清单）",
     "source /opt/ros/melodic/setup.bash 2>/dev/null; "
     "rosnode list 2>&1 | head -12 || echo '(rosnode 不可用)'"),
    ("关节实时读数（机械臂是否活着）",
     "source /opt/ros/melodic/setup.bash 2>/dev/null; "
     "timeout 6 rostopic echo -n1 /joint_states 2>&1 | head -20 || echo '(读不到 /joint_states)'"),
    ("控制接口是否存在（ros_control 的 action 是 topic 外的另一套）",
     "source /opt/ros/melodic/setup.bash 2>/dev/null; "
     "rosservice list 2>/dev/null | grep -E "
     "'arm_controller|gripper_controller' | head -6; "
     "echo '--- action server ---'; "
     "rostopic list 2>/dev/null | grep -E 'follow_joint_trajectory' | head -6 || true; "
     "echo '--- controller_manager ---'; "
     "rosservice list 2>/dev/null | grep controller_manager | head -3 || echo '(无)'"),
    ("厂商 SDK 是否已部署（对照资料应为 24,966 字节）",
     "ls -l ~/ros_robot_controller_sdk.py 2>/dev/null || "
     "find ~ -name 'ros_robot_controller_sdk.py' 2>/dev/null | head -3 || echo '(未找到 SDK)'"),
    ("板载串口枚举（坑 1：不存在 ttyUSB* 是正常的）",
     "ls /dev/tty* 2>/dev/null | head -10"),
    ("串口占用（坑 3：双通道超时的真因常是厂商节点占着 ttyS0）",
     "sudo -n fuser -v /dev/ttyS0 2>&1 | head -6 || "
     "fuser -v /dev/ttyS0 2>&1 | head -6 || echo '(看不到占用)'"),
    ("厂商 bringup 进程",
     "ps aux | grep -iE 'ArmPi|roslaunch|roscore|hiwonder' | grep -v grep | head -6 || echo '(无)'"),
    ("STA 出网能力（第 14 项的关键判据）",
     "ip -4 addr show wlan0 2>/dev/null | grep -E 'inet ' ; "
     "echo '--- 默认路由 ---'; ip route | grep default || echo '(无默认路由 → 无法出网)'"),
    ("出网实测（能 ping 通才算真通）",
     "timeout 6 ping -c 2 -W 2 223.5.5.5 2>&1 | tail -2; "
     "echo '--- DNS ---'; timeout 6 getent hosts www.huaweicloud.com || echo '(DNS 解析失败)'"),
    ("mosquitto 安装状态（第 15 项）",
     "command -v mosquitto >/dev/null && mosquitto -h 2>&1 | head -1 || echo '(mosquitto 未安装)'"),
    ("paho-mqtt（bridge 依赖）",
     "python3 -c 'import paho.mqtt.client; print(\"paho-mqtt OK\")' 2>&1 | tail -1"),
)


def _tcp_open(host: str, port: int, timeout: float = 2.5) -> bool:
    """纯 TCP 握手，不发任何数据。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# 上传到树莓派执行的只读探针。
#
# 为什么要走 SFTP 上传而不是 `python3 -c "..."`：多行脚本经 shell 传过去，
# 换行会被转义成字面量 \n，直接 SyntaxError。第一版就是这么坏的 —— 症状是
# "检查项静默失败"，比报错更坏，因为它看起来像"读不到舵机"。
#
# ★ 树莓派是 Python 3.6：**不能用 subprocess.run(capture_output=)**
#   （3.7+ 才有），也不能用 f-string 里的 = 语法。已在 2026-10-05 实跑验证。
#
# 三个安全约束：
#   1. 只读 /dev/ttyAMA0（厂商 hiwonder_servo_manager 占着 ttyS0）
#   2. 所有 SDK 读调用走 work_queue（多线程 + 超时）—— SDK 内部的
#      queue.get(block=True) 没有超时，回包不来就永久卡死
#   3. 不调用任何 set_* / enable_torque，不下发任何运动
SERVO_PROBE = r'''
import sys, threading, queue, time
sys.path.insert(0, "/home/ubuntu")
import ros_robot_controller_sdk as rrc

WORK = queue.Queue(maxsize=32)
TIMEOUT = 6.0

def call(fn, *a):
    """in 独立线程里跑 SDK 读调用，超时就放弃，绝不无限等。"""
    box = {}
    def run():
        try:
            box["v"] = fn(*a)
        except Exception as e:
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        box["r"] = WORK.get(timeout=TIMEOUT)
    except queue.Empty:
        return "(timeout %ds)" % TIMEOUT
    if box.get("e") is not None:
        return "error: %s" % box["e"]
    return box.get("v")

board = rrc.Board(device="/dev/ttyAMA0", baudrate=1000000, timeout=3)
board.enable_reception(True)
time.sleep(0.8)

print("reception enabled =", board.enable_recv, flush=True)
print("活跃线程:", [t.name for t in threading.enumerate()], flush=True)

# 先测裸串口有没有字节进来 —— 不经过 SDK 协议层，能区分
# "总线上没设备" 与 "协议层不对"
board.port.reset_input_buffer()
time.sleep(1.0)
idle = board.port.read(64)
print("静默 1s 裸串口读到 %d 字节" % len(idle), flush=True)

# 发只读的广播读 ID 指令，看有没有回包
board.buf_write(rrc.PacketFunction.PACKET_FUNC_BUS_SERVO, [0x12, 254])
time.sleep(1.5)
reply = board.port.read(64)
print("发 read_id 广播后读到 %d 字节: %s" % (len(reply), list(reply[:24])), flush=True)

if len(reply) == 0:
    print("")
    print("!! 舵机对 ttyAMA0 的指令零应答 —— 这是 2026-10-05 的实测结论：")
    print("   厂商 hiwonder_servo_manager(pid 8884) 持有 /dev/ttyS0，")
    print("   舵机实际由那条链路控制，扩展板只应答它。")
    print("   => 走 ROS 控制栈(方案A)，不要抢 ttyS0；也别指望 ttyAMA0 直连。")
else:
    ids = call(board.bus_servo_read_id)
    print("")
    print("在线舵机 ID: %s" % (ids,), flush=True)
    for i in (ids if isinstance(ids, (list, tuple)) and ids else [1,2,3,4,5,6]):
        pos = call(board.bus_servo_read_position, i)
        vin = call(board.bus_servo_read_vin, i)
        tmp = call(board.bus_servo_read_temp, i)
        lim = call(board.bus_servo_read_angle_limit, i)
        print("  id=%-4s pos=%-16s vin=%-8s temp=%-8s angle_limit=%s" % (i, pos, vin, tmp, lim))
        while not WORK.empty():
            WORK.get_nowait()
print("")
print("pos 量程 0-1000(脉宽)；angle_limit 是真实关节上下限")
'''


def _run_servo_probe(cli) -> str:  # noqa: ANN001
    """把探针传到树莓派跑一次，输出其 stdout。"""
    remote = "/tmp/_oceanus_servo_probe.py"
    try:
        sftp = cli.open_sftp()
        with sftp.open(remote, "w") as handle:
            handle.write(SERVO_PROBE)
        sftp.close()
    except Exception as exc:  # noqa: BLE001
        return "(上传失败：%s)" % exc
    try:
        _, so, se = cli.exec_command(
            "timeout 70 python3 %s 2>&1" % remote, timeout=90
        )
        so.channel.settimeout(90)
        out = so.read().decode("utf-8", "replace")
        err = se.read().decode("utf-8", "replace")
        return (out + ("\n[stderr] " + err if err.strip() else "")).rstrip()
    except Exception as exc:  # noqa: BLE001
        return "(执行失败：%s)" % exc
    finally:
        try:
            cli.exec_command("rm -f %s" % remote, timeout=10)
        except Exception:  # noqa: BLE001
            pass


def layer_ports(host: str) -> int:
    """第 1 层：免密端口探测。"""
    print("=" * 68)
    print("第 1 层 · 免密端口探测（只做 TCP 握手，不发数据）")
    print("=" * 68)

    reachable = _tcp_open(host, 22, timeout=1.5)
    if not reachable:
        print("  [!] 22 端口不通，后两层无法进行。")
        print("      排查：① 电脑是否连上树莓派热点 ② 树莓派是否开机 ③ IP 是否变化")
        return 1

    print(f"  目标 {host}\n")
    for probe in PORTS:
        state = "OPEN" if _tcp_open(host, probe.port) else "closed"
        mark = "✓" if state == "OPEN" else "·"
        print(f"  {mark} {probe.port:>5}  {state:<7} {probe.label}")
        print(f"          {probe.why}")
    return 0


def layer_ssh(host: str, user: str, password: str) -> int:
    """第 2 层：SSH 只读体检。"""
    try:
        import paramiko
    except ImportError:
        print("\n[!] 缺 paramiko，跳过 SSH 层。安装：pip install paramiko cryptography")
        return 0

    try:
        cli = paramiko.SSHClient()
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        cli.connect(host, username=user, password=password, timeout=15,
                    look_for_keys=False, allow_agent=False)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[!] SSH 连接失败：{type(exc).__name__}: {exc}")
        return 1


    print("\n" + "=" * 68)
    print("第 2 层 · SSH 只读体检（不驱动任何关节）")
    print("=" * 68)

    with cli:
        for title, cmd in SSH_CHECKS:
            print(f"\n── {title} " + "─" * max(0, 56 - len(title)))
            try:
                _, so, _ = cli.exec_command(cmd, timeout=25)
                so.channel.settimeout(25)
                out = so.read().decode("utf-8", "replace").rstrip()
            except Exception as exc:  # noqa: BLE001
                out = f"(timeout / {type(exc).__name__})"
            print("  " + (out.replace("\n", "\n  ") if out else "(空输出)"))

        # 多行 Python 用 SFTP 上传执行 —— 走 shell -c 传内联脚本会被
        # 转义吃掉（\n 变成字面量），第一版就是这么坏的。
        print(f"\n── ★ 在线舵机 ID / 电压 / 温度 " + "─" * 30)
        out = _run_servo_probe(cli)
        print("  " + (out.replace("\n", "\n  ") if out.strip() else "(无输出)"))

    print("\n" + "=" * 68)
    print("体检结束。安全边界：本脚本未下发任何运动指令。")
    print("抓取姿态示教仍必须由人在机械臂旁手动摆位并填入 config.yaml。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="机械臂到货一键体检（纯只读）")
    ap.add_argument("--host", default=os.environ.get("PI_HOST", "192.168.149.1"))
    ap.add_argument("--user", default=os.environ.get("PI_USER", "ubuntu"))
    ap.add_argument("--ports-only", action="store_true", help="只跑免密端口层")
    ap.add_argument("--json", action="store_true", help="端口层结果以 JSON 输出")
    args = ap.parse_args()

    started = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"机械臂到货体检 · {started} · 目标 {args.host}")
    print("安全边界：全部只读，不驱动关节\n")

    if args.json:
        payload = {
            "host": args.host,
            "checked_at": started,
            "ports": {str(p.port): _tcp_open(args.host, p.port) for p in PORTS},
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    rc = layer_ports(args.host)
    if args.ports_only:
        return rc

    password = os.environ.get("PI_PASSWORD", "")
    if not password:
        print("\n" + "=" * 68)
        print("第 2/3 层跳过：未设置 PI_PASSWORD")
        print("要继续：set PI_PASSWORD=<你的树莓派密码> 然后重跑本脚本")
        return rc

    return layer_ssh(args.host, args.user, password)


if __name__ == "__main__":
    sys.exit(main())
