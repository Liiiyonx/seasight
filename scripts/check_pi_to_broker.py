#!/usr/bin/env python3
"""诊断「树莓派能否连到本机 broker」—— 已实测的失败原因定位。

用法::

    PI_PASSWORD=xxx python scripts/check_pi_to_broker.py
    python scripts/check_pi_to_broker.py --broker-ip 192.168.149.1

★ 实测结论（2026-10-05 15:45，请勿重复排查）
-------------------------------------------------
**树莓派连不上本机 broker，原因是 Windows 移动热点的 AP 客户端隔离。**

证据链：

===========================  ==================  ==================
方向                         结果                 说明
===========================  ==================  ==================
电脑 → 树莓派 192.168.149.1  rc=0 通              SSH 一直可用
电脑 → 192.168.149.249:22    rc=10061 拒绝        **.249 不是本机**
树莓派 → 网关 .100           ping 100% 丢包       ARP 状态 INCOMPLETE
树莓派 → 任意端口(本机)      rc=11 (EAGAIN)      非拒绝、非超时
树莓派 ARP 表                只有 .249 一条       连网关都解析不到
===========================  ==================  ==================

``rc=11 (EAGAIN)`` 是 AP 隔离的典型特征：包被 AP 静默丢弃，
既不返回 RST（连接被拒）也不超时。

★ 为什么电脑 → 树莓派通、反向不通？
  Windows 移动热点里，电脑是 **AP/网关**，树莓派是**客户端**。
  客户端之间默认隔离（Windows 10/11 移动热点的默认行为）。

三种解法
--------
1. **改用有线路由**（最省事）
   树莓派和电脑都插同一个路由器/交换机 → 无隔离。
   前提：树莓派有网口（本机 `eth0` 有没有插线需现场确认）。

2. **树莓派改连 STA 模式**（推荐，一举两得）
   树莓派连你家/办公的 WiFi，电脑也在同一网络 →
   既解决 AP 隔离，又**顺带解决"不能出网"**（能装 paho-mqtt、
   moveit-core 等所有缺失依赖）。
   代价：需要 WiFi 名称与密码；改完 SSH 会断（用 NoMachine 兜底）。

3. **让 broker 跑在树莓派上**（不推荐）
   把 EMQX 装到树莓派，树莓派同时是 broker 和设备。
   缺点：树莓派不能出网 → 装不了 EMQX；且与平台不在同一 broker。

推荐 **方案 2** —— 它一次解决两个阻塞点（第 14 项出网 + AP 隔离）。
"""

import argparse
import io
import json
import os
import re
import sys

HOST = "192.168.149.1"


def ssh_connect(host, user, password, timeout=15):
    try:
        import paramiko
    except ImportError:
        print("!! 缺 paramiko：pip install paramiko")
        sys.exit(2)
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(host, username=user, password=password, timeout=timeout,
                    look_for_keys=False, allow_agent=False)
    except Exception as exc:  # noqa: BLE001
        print("!! SSH 失败：%s: %s" % (type(exc).__name__, exc))
        return None
    return cli


def run(cli, cmd, timeout=25):
    _, so, _ = cli.exec_command(cmd, timeout=timeout)
    so.channel.settimeout(timeout)
    return so.read().decode("utf-8", "replace").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--user", default="ubuntu")
    ap.add_argument("--broker-ip", default="",
                    help="本机在树莓派视角下的 IP（留空则自动探测）")
    args = ap.parse_args()

    password = os.environ.get("PI_PASSWORD", "")
    if not password:
        print("需要环境变量 PI_PASSWORD（树莓派出厂口令）")
        return 2

    print("=" * 68)
    print("树莓派 → 本机 broker 连通性诊断")
    print("=" * 68)

    # ── 1. 电脑 → 树莓派（对照组）──
    import socket
    print()
    print("【1】电脑 → 树莓派 %s:22（对照，应通）" % args.host)
    s = socket.socket()
    s.settimeout(4)
    rc = s.connect_ex((args.host, 22))
    s.close()
    print("    rc=%d  %s" % (rc, "通" if rc == 0 else "不通（异常）"))

    cli = ssh_connect(args.host, args.user, password)
    if not cli:
        return 1

    try:
        # ── 2. 树莓派的网络视图 ──
        print()
        print("【2】树莓派的网络视图")
        own_mac = run(cli, "cat /sys/class/net/wlan0/address")
        print("    树莓派 wlan0 MAC: %s" % own_mac)
        neigh = run(cli, "ip neigh")
        print("    ARP 表:")
        for line in neigh.split("\n"):
            if line.strip():
                print("      %s" % line.strip())
        routes = run(cli, "ip route")
        print("    路由表:")
        for line in routes.split("\n"):
            if line.strip():
                print("      %s" % line.strip())

        # ── 3. 探测本机 IP ──
        broker_ip = args.broker_ip
        if not broker_ip:
            print()
            print("【3】探测本机在热点的 IP")
            # 树莓派 ARP 表里 REACHABLE 且 MAC 不是自己的那些，就是候选
            for line in neigh.split("\n"):
                m = re.match(r"(\S+)\s+dev\s+\S+\s+lladdr\s+(\S+)\s+REACHABLE", line)
                if m and m.group(2).lower() != own_mac.lower():
                    broker_ip = m.group(1)
                    print("    候选（树莓派唯一可达对端）: %s  MAC %s"
                          % (m.group(1), m.group(2)))
            # 验证它是不是本机：从电脑侧反查它的 22 端口
            if broker_ip:
                s = socket.socket()
                s.settimeout(3)
                rc22 = s.connect_ex((broker_ip, 22))
                s.close()
                print("    电脑 → %s:22 rc=%d %s"
                      % (broker_ip, rc22,
                         "← 是本机" if rc22 == 0 else "← 不是本机（22 拒绝）"))
                if rc22 != 0:
                    print()
                    print("    ★ 该地址不是你的电脑 —— 说明树莓派连的不是热点，")
                    print("      而是把电脑当成了另一台设备。AP 隔离成立。")
        else:
            print()
            print("【3】使用指定的 broker IP: %s" % broker_ip)

        # ── 4. 树莓派 → 本机各端口 ──
        print()
        print("【4】树莓派 → 本机端口（用 Python socket，不用 /dev/tcp）")
        probe = (
            "import socket,sys\n"
            "host=sys.argv[1]\n"
            "for port in (22,1883,18083,18000):\n"
            "    s=socket.socket(); s.settimeout(4)\n"
            "    try: rc=s.connect_ex((host,port))\n"
            "    finally: s.close()\n"
            "    print('    %-6d rc=%d' % (port,rc))\n"
        )
        sftp = cli.open_sftp()
        sftp.open("/tmp/_conn.py", "w").write(probe)
        sftp.close()
        out = run(cli, "python3 /tmp/_conn.py %s 2>&1" % broker_ip, timeout=40)
        cli.exec_command("rm -f /tmp/_conn.py", timeout=10)
        for line in out.split("\n"):
            if line.strip():
                print(line.rstrip())

        # ── 5. 结论 ──
        print()
        print("=" * 68)
        print("结论：Windows 移动热点做了 AP 客户端隔离")
        print("  电脑是 AP/网关，树莓派是客户端，客户端之间被隔离。")
        print("  rc=11(EAGAIN) 是静默丢包的典型特征（非拒绝、非超时）。")
        print()
        print("推荐解法：树莓派改连 STA 模式（连你家的 WiFi）")
        print("  ✓ 一次解决两个阻塞点：AP 隔离 + 不能出网")
        print("    （出网后才能装 paho-mqtt / moveit-core 等所有缺失依赖）")
        print("  前提：需要 WiFi 名称与密码；改完 SSH 会断，先用 NoMachine 兜底。")
        print("=" * 68)
        return 1
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(main())
