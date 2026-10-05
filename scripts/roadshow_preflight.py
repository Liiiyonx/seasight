#!/usr/bin/env python3
"""路演现场一键体检：60 秒内判断「今天的网络能不能用」。

★ 核心问题
----------
路演现场只有**手机热点**，没有网线。手机热点作为 AP，
**其内部两个客户端（电脑与树莓派）之间能否通信，
取决于手机型号与系统版本，无法在事前保证**。

所以路演方案**不能建立在「热点内点对点可达」这个假设上**。

★ 唯一可靠的架构
----------------
让流量**走公网**，而不是走热点内部的局域网::

    树莓派 ──WiFi──► 手机热点 ──NAT──► 互联网 ──443──► 云端 broker
    电脑   ──WiFi──► 手机热点 ──NAT──► 互联网 ──443──► 云端平台

这样手机热点的「客户端隔离」**完全不影响** ——
因为树莓派和电脑都不是在跟对方通信，而是在各自访问公网。

★ 本脚本做三件事
----------------
1. 查树莓派是否连上了手机热点、能否出网
2. 测手机热点内「树莓派 → 电脑」是否通（通则本机方案也能用，更省事）
3. 测树莓派 → 云端 WSS 是否通（★ 这才是路演真正依赖的那条）

用法::

    # 开发环境（用 .env）
    PI_PASSWORD=xxx PI_CLOUD_HOST=<ECS域名> python scripts/roadshow_preflight.py

    # 路演现场（用云端凭据）
    PI_PASSWORD=xxx PI_CLOUD_HOST=<ECS域名> \\
    PI_CLOUD_USERNAME=robot_device PI_CLOUD_PASSWORD=<密码> \\
        python scripts/roadshow_preflight.py
"""

import argparse
import base64
import json
import os
import sys
import tempfile

PI_HOST = "192.168.149.1"


def sh(cli, cmd, timeout=30):
    _, so, _ = cli.exec_command(cmd, timeout=timeout)
    so.channel.settimeout(timeout)
    return so.read().decode("utf-8", "replace").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=PI_HOST)
    ap.add_argument("--user", default="ubuntu")
    ap.add_argument("--pi-password", default=os.environ.get("PI_PASSWORD", ""))
    ap.add_argument("--cloud-host", default=os.environ.get("PI_CLOUD_HOST", ""),
                    help="云端平台域名（路演必填）")
    ap.add_argument("--cloud-port", type=int,
                    default=int(os.environ.get("PI_CLOUD_PORT", "443")))
    ap.add_argument("--skip-pi", action="store_true",
                    help="跳过树莓派相关检查（只测本机）")
    args = ap.parse_args()

    cloud = args.cloud_host.strip()
    if not cloud:
        print("!! 未提供 --cloud-host")
        print("   路演依赖「树莓派 → 云端 WSS」这条路径，不填无法判断。")

    print("=" * 68)
    print("路演现场体检")
    print("=" * 68)
    print("  判定逻辑：流量走公网（NAT）而非热点内点对点，")
    print("            这样手机热点的客户端隔离不影响我们。")
    print()

    # ── 1. 本机侧 ──
    print("[1] 本机（电脑）")
    try:
        import paho.mqtt.client as mqtt  # noqa: F401
        print("    ✓ paho-mqtt 已装")
    except ImportError:
        print("    ✗ 缺 paho-mqtt —— pip install paho-mqtt")
    if cloud:
        import socket
        s = socket.socket()
        s.settimeout(8)
        rc = s.connect_ex((cloud, args.cloud_port))
        s.close()
        print("    %s 云端 %s:%d"
              % ("✓" if rc == 0 else "✗", cloud, args.cloud_port))

    if args.skip_pi:
        return 0

    # ── 2. 树莓派侧 ──
    if not args.pi_password:
        print()
        print("!! 未提供 PI_PASSWORD，无法检查树莓派")
        return 2

    try:
        import paramiko
    except ImportError:
        print("!! 缺 paramiko：pip install paramiko")
        return 2

    print()
    print("[2] 树莓派")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(args.host, username=args.user, password=args.pi_password,
                    timeout=15, look_for_keys=False, allow_agent=False)
    except Exception as exc:  # noqa: BLE001
        print("    ✗ SSH 失败: %s: %s" % (type(exc).__name__, exc))
        print("      → 树莓派没连上热点？检查它连的是哪个 WiFi。")
        return 1
    print("    ✓ SSH 通")

    # 连的是哪个 WiFi
    ssid = sh(cli, "nmcli -t -f SSID dev wifi 2>/dev/null | head -1 "
                    "|| iwconfig wlan0 2>/dev/null | grep ESSID | "
                    "awk '{print substr($2,2)}'")
    print("    当前 WiFi: %s" % (ssid or "(查不到)"))
    if ssid and ssid.lower() not in ("", "unknown"):
        print("    ✓ 看起来已连上热点")

    # 能否出网
    route = sh(cli, "ip route | grep default")
    print("    默认路由: %s" % (route or "★ 无（不能出网）"))
    ping = sh(cli, "timeout 6 ping -c 2 -W 2 223.5.5.5 2>&1 | tail -1")
    can_out = "2 received" in ping or "0% packet loss" in ping
    print("    %s 出网: %s" % ("✓" if can_out else "✗", ping[:60]))

    # 热点内点对点（本机方案能否走通 —— 通则更省事）
    print()
    print("[3] 热点内点对点（本机方案 —— 通则更省事，但不可依赖）")
    probe = """import socket, sys
pc = sys.argv[1]
for port in (1883, 18000):
    s = socket.socket(); s.settimeout(5)
    try:
        rc = s.connect_ex((pc, port))
    finally:
        s.close()
    print('    -> %s:%d  rc=%d %s' % (pc, port, rc,
          '通' if rc == 0 else '不通(隔离)'))
"""
    sftp = cli.open_sftp()
    tmp = os.path.join(tempfile.gettempdir(), "_pre_probe.py")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(probe)
    sftp.put(tmp, "/tmp/_pre_probe.py")
    sftp.close()

    # 电脑在树莓派视角的 IP：优先取热点网段的那个
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.149.1", 22))
        pc_ip = s.getsockname()[0]
    except Exception:
        pc_ip = "192.168.149.249"
    finally:
        s.close()
    print("    电脑在热点内的 IP: %s" % pc_ip)
    out = sh(cli, "timeout 25 python3 /tmp/_pre_probe.py %s 2>&1" % pc_ip)
    for line in out.split("\n"):
        if line.strip():
            print(line)
    cli.exec_command("rm -f /tmp/_pre_probe.py", timeout=10)

    # ★ 关键：云端 WSS
    print()
    print("[4] ★ 树莓派 → 云端 WSS（路演真正依赖的路径）")
    if not cloud:
        print("    跳过（未提供 --cloud-host）")
        cli.close()
        return 2

    user = (os.environ.get("PI_CLOUD_USERNAME")
            or os.environ.get("ARM_MQTT_USERNAME") or "robot_device")
    pw = (os.environ.get("PI_CLOUD_PASSWORD")
          or os.environ.get("ARM_MQTT_PASSWORD") or "")

    wss_probe = '''import sys, time
sys.path.insert(0, "/home/ubuntu/.local/lib/python3.6/site-packages")
import paho.mqtt.client as mqtt

host, port, path, user, pw = sys.argv[1:6]
state = {}
def on_connect(c, u, f, rc):
    state["rc"] = rc
try:
    c = mqtt.Client(client_id="preflight", transport="websockets")
    c.ws_set_options(path=path,
                     headers={"Sec-WebSocket-Protocol": "mqtt"})
    c.username_pw_set(user, pw)
    c.on_connect = on_connect
    c.connect(host, int(port), 15)
    c.loop_start()
    dl = time.time() + 20
    while time.time() < dl and "rc" not in state:
        time.sleep(0.2)
    print("    WSS CONNACK =", state.get("rc"),
          "(0=通, 4=密码错, None=连不上)")
except Exception as e:
    print("    WSS 异常:", type(e).__name__, str(e)[:100])
'''
    sftp = cli.open_sftp()
    tmp2 = os.path.join(tempfile.gettempdir(), "_wss_probe.py")
    with open(tmp2, "w", encoding="utf-8") as f:
        f.write(wss_probe)
    sftp.put(tmp2, "/tmp/_wss.py")
    sftp.close()
    if not pw:
        print("    ! 未提供密码，只能测连通性不能测认证")
    out = sh(cli, "python3 /tmp/_wss.py %s %d /mqtt %s %s 2>&1"
             % (cloud, args.cloud_port, user, pw or "x"), timeout=45)
    for line in out.split("\n"):
        if line.strip():
            print(line)
    cli.exec_command("rm -f /tmp/_wss.py", timeout=10)
    cli.close()

    print()
    print("=" * 68)
    print("现场决策表")
    print("=" * 68)
    print("  树莓派能出网 + 云端 WSS 通")
    print("    → ★ 用云端方案。手机热点隔离完全不影响（走公网）。")
    print("  热点内点对点也通")
    print("    → 可以用本机方案，更省事（不用连公网）。")
    print("  两者都不通")
    print("    → 检查手机热点：最大连接数、2.4G 频段、密码。")
    print()
    print("  兜底（完全不需要网络）：NoMachine(4000) + 厂商上位机 GUI 演示")
    return 0


if __name__ == "__main__":
    sys.exit(main())
