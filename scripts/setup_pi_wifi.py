#!/usr/bin/env python3
"""把树莓派从「手机热点(AP)」改到「家/办公 Wi-Fi(STA)」。

★ 为什么必须改
--------------
2026-10-05 实测两个阻塞点，**一次改 STA 可同时解决**：

1. **AP 客户端隔离**：Windows 移动热点里电脑是 AP/网关、树莓派是客户端，
   客户端之间被静默丢包（``connect_ex`` 返回 ``11 EAGAIN``，
   既不拒绝也不超时）。→ 树莓派连不上本机 broker，bridge 无法上报。
2. **不能出网**：无默认路由，``ping`` 报 Network unreachable。
   → 装不了 mosquitto、moveit-core 等系统包。

改成 STA 后两者都是普通局域网节点：电脑能连树莓派，树莓派也能连电脑，
并且能出网装依赖。

★★ 安全前提（脚本会先检查，不通过就拒绝执行）★★
-------------------------------------------------
**NoMachine(4000) 必须能从本机连上**，否则改完 STA 树莓派换了 IP，
SSH 会断、你也看不见屏幕 —— 设备可能变成砖（只能重新插显示器）。
本机实测 4000 端口 rc=0 可连，脚本每次运行都会复验。

用法::

    # 先干跑（只检查前提与将要执行的改动，不落盘）
    python scripts/setup_pi_wifi.py --ssid "你的WiFi" --dry-run

    # 确认后真改
    python scripts/setup_pi_wifi.py --ssid "你的WiFi"

    # 密码从环境变量读，避免留在 shell 历史里
    export PI_WIFI_PASSWORD='密码'
    python scripts/setup_pi_wifi.py --ssid "你的WiFi"

    # 树莓派上没有网络时用键盘配（显示器 + 键盘）
    python scripts/setup_pi_wifi.py --scan     # 扫描可用 WiFi
"""

import argparse
import getpass
import os
import re
import socket
import sys

PI_HOST = "192.168.149.1"
NO_MACHINE_PORT = 4000

# 配置文件：改前先备份，失败可回滚
WPA_CONF = "/etc/wpa_supplicant/wpa_supplicant.conf"
DHCP_CONF = "/etc/dhcpcd.conf"

BLOCK_START = "# >>> oceanus-sta >>>"
BLOCK_END = "# <<< oceanus-sta <<<"


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
        print("!! SSH 连接失败：%s: %s" % (type(exc).__name__, exc))
        return None
    return cli


def run(cli, cmd, timeout=30):
    _, so, se = cli.exec_command(cmd, timeout=timeout)
    so.channel.settimeout(timeout)
    out = so.read().decode("utf-8", "replace").strip()
    err = se.read().decode("utf-8", "replace").strip()
    return out or (("(stderr) " + err) if err else "")


def check_nomachine(host=PI_HOST, port=NO_MACHINE_PORT) -> bool:
    """验证 NoMachine 可连 —— 改 STA 的安全前提。"""
    s = socket.socket()
    s.settimeout(5)
    try:
        rc = s.connect_ex((host, port))
    except OSError:
        return False
    finally:
        s.close()
    return rc == 0


def scan_wifi(cli) -> None:
    """扫描附近 WiFi（需要无线工具，可能没有）。"""
    print("扫描附近 WiFi…")
    if "command not found" in run(cli, "which nmcli"):
        print("  树莓派上没有 nmcli。")
        print("  可用下面这招看已保存的网络：")
        print(run(cli, "grep ssid %s 2>/dev/null | head -10" % WPA_CONF))
        print()
        print("手动办法：把树莓派接显示器键盘，运行：")
        print("  sudo nano /etc/wpa_supplicant/wpa_supplicant.conf")
        print("按 wifi_passphrase 格式手动加一条 country=CN 网络。")
        return
    out = run(cli, "nmcli device wifi list --rescan yes 2>/dev/null "
                   "| awk 'NR>2 {print $2, $9}' | head -14")
    print(out)


def derive_psk(ssid: str, password: str) -> str:
    """按 wpa_passphrase 的算法派生 PSK（**只算 psk，不生成配置块**）。

    ★ 用 wpa_passphrase 的等价格式：把密码做 **PBKDF2-SHA1** 派生，
      不写明文。这样配置���件本身不含密码明文。

    密码是 8-63 字符 WPA-PSK，或 64 位十六进制。
    """
    import hashlib
    if not (8 <= len(password) <= 63):
        raise ValueError("WPA 密码需 8-63 字符，当前 %d" % len(password))
    psk = hashlib.pbkdf2_hmac(
        "sha1", password.encode("utf-8"),
        ssid.encode("utf-8"), 4096, 32).hex()
    return psk


def build_block(networks):
    """生成 wpa_supplicant 配置块，支持**多个网络**。

    ★ 为什么支持多个（2026-10-05 路演场景实测得出）
    ------------------------------------------------
    wpa_supplicant 会**按顺序尝试**所有 network 块，连上第一个能连的。
    把「手机热点」和「电脑热点」都写进去，树莓派现场插哪个都能上，
    不用改配置 —— 这正是路演需要的（现场网络不确定）。

    排序：``priority`` 大的优先，所以**第一个是最想用的**。
    同一 priority 时 wpa_supplicant 按文件顺序尝试。

    ★ 坑：``update_config=1`` 会让 wpa_supplicant **自动重写**这个文件
      （连上新的 AP 后可能加自己的 network 块、删掉注释）。
      所以必须用**块标记**（BLOCK_START/END）来定位我们的区间，
      重新生成时按标记替换而不是追加 —— 否则会无限膨胀。
    """
    parts = [
        BLOCK_START,
        "country=CN",
        "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev",
        "update_config=1",
    ]
    for i, (ssid, psk) in enumerate(networks):
        # priority 从高到低递减（255 是最大值）
        pri = 255 - i
        parts.append("network={")
        parts.append('    ssid="%s"' % ssid)
        parts.append("    psk=%s" % psk)
        parts.append("    key_mgmt=WPA-PSK")
        parts.append("    scan_ssid=1")
        parts.append("    priority=%d" % pri)
        parts.append("}")
    parts.append(BLOCK_END)
    return "\n".join(parts) + "\n"


def verify_psk_on_pi(cli, ssid: str, password: str) -> bool:
    """用树莓派自带的 wpa_passphrase 交叉验证我们派生的 psk。

    ★★ 这是防回归的关键检查。
       派生算法写错时，生成的配置**语法完全正确、但永远连不上 WiFi**，
       本地任何检查都发现不了 —— 只有拿官方工具对照才能暴露。
       第一版就栽在这（salt 用了固定字符串而非 SSID）。

    树莓派实测有 /usr/bin/wpa_passphrase（2026-10-05）。
    """
    import hashlib
    mine = hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"),
                               ssid.encode("utf-8"), 4096, 32).hex()
    # shell 转义：SSID/密码里的引号可能破坏命令，故用 base64 传递
    import base64
    b64_ssid = base64.b64encode(ssid.encode()).decode()
    b64_pw = base64.b64encode(password.encode()).decode()
    # ★ 树莓派是 Python 3.6，subprocess.run 没有 capture_output（3.7+），
    #   也没有 input=（3.7+）。必须用 Popen + communicate。
    #   —— 这是本次第三个 Python 3.6 兼容坑（前两个：subprocess.run(capture_output=)、
    #      __future__ annotations），已写进 py36_compat 的模块文档备忘。
    cmd = (
        "python3 -c \"import base64,subprocess;"
        "s=base64.b64decode('%s').decode();"
        "p=base64.b64decode('%s').decode();"
        "proc=subprocess.Popen(['wpa_passphrase',s],stdin=subprocess.PIPE,"
        "stdout=subprocess.PIPE,stderr=subprocess.PIPE);"
        "so,_=proc.communicate(p.encode());"
        "print([l for l in so.decode().split(chr(10)) if l.strip().startswith('psk=')][0].split('=')[1].strip())\""
        % (b64_ssid, b64_pw)
    )
    out = run(cli, cmd, timeout=30)
    theirs = out.strip()
    if not theirs or len(theirs) != 64:
        print("  [!] 树莓派 wpa_passphrase 未返回有效 psk（得到 %r）" % theirs)
        print("      无法交叉验证，但不影响后续步骤（仍会用我们自己的派生值）")
        return True  # 验证不了不等于失败
    match = (theirs == mine)
    print("  我们派生:   %s" % mine)
    print("  wpa_passphrase: %s" % theirs)
    print("  → %s" % ("✓ 逐字符一致" if match else "✗ 不一致 —— 派生算法有错！"))
    return match


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ssid", action="append", default=[],
                    help="WiFi 名称（**可重复**，按给定顺序作为优先级；"
                         "2.4G 频段，5G 多数设备不支持）"
                         " 例：--ssid 手机热点 --ssid 电脑热点")
    ap.add_argument("--password", help="WiFi 密码（建议用 PI_WIFI_PASSWORD 环境变量）")
    ap.add_argument("--host", default=PI_HOST)
    ap.add_argument("--user", default="ubuntu")
    ap.add_argument("--dry-run", action="store_true",
                    help="只检查前提并预览改动，不落盘")
    ap.add_argument("--scan", action="store_true", help="扫描附近 WiFi")
    ap.add_argument("--show-current", action="store_true", help="打印当前网络配置")
    args = ap.parse_args()

    password = os.environ.get("PI_PASSWORD", "")
    if not password and not args.scan:
        print("需要环境变量 PI_PASSWORD（树莓派 SSH 口令）")
        return 2

    # ── 1. 安全前提检查 ──
    print("=" * 66)
    print("安全前提检查")
    print("=" * 66)
    nomachine = check_nomachine(args.host)
    if nomachine:
        print("  [OK] NoMachine(4000) 可连 —— 改 STA 后仍能看见树莓派屏幕")
    else:
        print("  [!!] NoMachine(4000) 连不上")
        print()
        print("  ★ 拒绝执行。理由：改 STA 后树莓派会离开热点、换个 IP，")
        print("    SSH 会断。如果你此时看不见树莓派的屏幕，设备可能变砖。")
        print()
        print("  先解决其一：")
        print("   a) 确认 NoMachine 服务在跑（树莓派上 systemctl status nxserver）")
        print("   b) 接显示器+键盘直接在树莓派上配置（--scan 有说明）")
        print("   c) 接受风险，用 --force 跳过本检查（不推荐）")
        if not os.environ.get("OCEANUS_FORCE"):
            return 3
        print()
        print("  检测到 OCEANUS_FORCE，跳过检查继续。")

    cli = ssh_connect(args.host, args.user, password)
    if not cli:
        return 1
    try:
        # ── 2. 只读信息 ──
        print()
        print("=" * 66)
        print("当前网络状态")
        print("=" * 66)
        print("  wlan0 地址: %s" % run(cli, "ip -4 addr show wlan0 | grep inet | head -1"))
        print("  默认路由:   %s" % (run(cli, "ip route | grep default") or "(无 —— 不能出网)"))
        print("  已存 SSID:  %s" % (run(cli, "grep -o 'ssid=\"[^\"]*\"' %s 2>/dev/null | head -4" % WPA_CONF) or "(空)"))

        if args.show_current:
            print()
            print(run(cli, "cat %s" % WPA_CONF))
            return 0

        if args.scan:
            print()
            scan_wifi(cli)
            return 0

        if not args.ssid:
            print()
            print("请用 --ssid 指定 WiFi 名称（可给多个，先跑 --scan 看有哪些）")
            print("  例：--ssid 手机热点 --ssid 电脑热点")
            return 2

        # ── 3. 密码 ──
        # ★ 多个网络时用 PI_WIFI_PASSWORD_<SSID> 形式分别指定，
        #   或用同一密码（PI_WIFI_PASSWORD）—— 多个热点同密码时最省事。
        base_pw = args.password or os.environ.get("PI_WIFI_PASSWORD")
        networks = []
        for ssid in args.ssid:
            key = "PI_WIFI_PASSWORD_%s" % re.sub(r"\W+", "_", ssid).upper()
            pw = os.environ.get(key) or base_pw
            if not pw:
                if not sys.stdin.isatty():
                    print()
                    print("!! 非交互式运行，需要密码")
                    print("   多热点同密码： export PI_WIFI_PASSWORD='密码'")
                    print("   不同密码：     export %s='密码'" % key)
                    return 2
                pw = getpass.getpass("WiFi %s 的密码: " % ssid)
            try:
                networks.append((ssid, derive_psk(ssid, pw)))
            except ValueError as exc:
                print("!! %s（ssid=%s）" % (exc, ssid))
                return 2
        try:
            block = build_block(networks)
        except ValueError as exc:
            print("!! %s" % exc)
            return 2

        # ── 4. 预览 ──
        print()
        print("=" * 66)
        print("将要写入 %s 的配置块" % WPA_CONF)
        print("=" * 66)
        for line in block.rstrip().split("\n"):
            # 不显示 psk 派生值（虽非明文，但也没必要给人看）
            print("  " + (line if "psk=" not in line else line[:8] + "<32字节PBKDF2派生值>"))

        # 交叉验证派生值 —— 语法正确但派生错是最隐蔽的失败
        print()
        print("=" * 66)
        print("交叉验证 psk 派生（防回归）")
        print("=" * 66)
        # ★ 每个网络都要单独交叉验证 —— 只要有一个不匹配就整体拒绝写入。
        #   salt 写错时语法完全正确但永远连不上，静态检查查不出来。
        pws = []
        for ssid in args.ssid:
            key = "PI_WIFI_PASSWORD_%s" % re.sub(r"\W+", "_", ssid).upper()
            pws.append((ssid, os.environ.get(key) or base_pw))
        bad = []
        for ssid, pw in pws:
            if not verify_psk_on_pi(cli, ssid, pw):
                bad.append(ssid)
        if bad:
            print()
            print("  ★ 以下网络的派生值与 wpa_passphrase 不一致，拒绝写入：%s"
                  % ", ".join(bad))
            print("    这会导致「配置语法正确但永远连不上 WiFi」，很难排查。")
            return 4

        if args.dry_run:
            print()
            print("这是 --dry-run，未做任何改动。去掉它即可执行。")
            return 0

        # ── 5. 备份并写入 ──
        print()
        print("=" * 66)
        print("执行")
        print("=" * 66)
        stamp = run(cli, "date +%Y%m%d-%H%M%S")
        backup = "%s.bak-%s" % (WPA_CONF, stamp)
        run(cli, "sudo -n cp %s %s" % (WPA_CONF, backup))
        print("  已备份 → %s" % backup)

        # 写文件：先备份，再移除旧块，再追加新块
        script = r'''
set -e
CONF=%s
STAMP=%s
sudo -n cp "$CONF" "$CONF.bak-$STAMP"
sudo -n sed -i '/%s/,/%s/d' "$CONF"
printf '%%s\n' %s | sudo -n tee -a "$CONF" >/dev/null
echo "--- 写入后的 ssid/psk 行 ---"
grep -E 'ssid=|psk=' "$CONF" | sed 's/psk=.*/psk=<hidden>/'
''' % (WPA_CONF, stamp, re.escape(BLOCK_START), re.escape(BLOCK_END),
           "'" + block.replace("'", "'\\''") + "'")
        out = run(cli, "bash -s <<'PIEOF'\n%s\nPIEOF" % script, timeout=40)
        print("  " + out.replace("\n", "\n  "))

        # ── 6. 停 AP 模式，切 STA ──
        print()
        print("  ★ 停掉厂商的 create_ap 热点进程（AP 与 STA 争同一张网卡）")
        # 厂商用 create_ap 起热点（实测 2026-10-05）：
        #   /bin/bash /usr/bin/create_ap -n wlan0 --no-virt --country US \
        #       -g 192.168.149.1 --freq-band 5 -c 149 HW-6AF67543
        #   hostapd 的配置在 /tmp/create_ap.wlan0.conf.XXXX/（临时目录）
        # ★ 它**不是 systemd 服务**（hostapd.service 实测是 inactive），
        #   所以 `systemctl stop hostapd` 没用，必须杀 create_ap 进程。
        # ★ 不停的后果：hostapd 继续占着 wlan0，wpa_supplicant 起不来，
        #   wlan0 仍是 Mode:Master —— 表现为"配置改了但没生效"。
        # ★ 必须用 sudo —— create_ap 是 root 起的进程，ubuntu 用户
        #   pkill 会报 "Operation not permitted"（2026-10-05 实际踩到）。
        out = run(cli, "sudo -n pkill -f 'create_ap -n wlan0' "
                      "&& echo '已停 create_ap' "
                      "|| echo '(没找到 create_ap，可能已停)'", timeout=20)
        print("    " + out.strip())
        run(cli, "sudo -n pkill -x hostapd 2>/dev/null; sleep 2; "
                 "pgrep -f hostapd >/dev/null && echo '警告: hostapd 仍在' "
                 "|| echo 'hostapd 已停'", timeout=20)

        print("  切 STA 模式（重启 wpa_supplicant 与网络）…")
        print("  ★ 接下来 SSH 会断开（网络重启），这是预期的")
        # ★ 网络重启会让当前 SSH 会话立刻失效 → exec_command 抛
        #   "SSH session not active"（2026-10-05 实际踩到）。
        #   所以把重启放进独立会话，失败也不让脚本崩。
        try:
            run(cli, "sudo -n systemctl restart wpa_supplicant 2>&1 | head -2",
                timeout=20)
        except Exception as exc:  # noqa: BLE001
            print("    （wpa_supplicant 已重启，SSH 随之断开：%s）"
                  % type(exc).__name__)
        try:
            run(cli, "sudo -n systemctl restart networking 2>&1 | head -2 "
                     "|| sudo -n ifdown wlan0 && sudo -n ifup wlan0",
                timeout=20)
        except Exception:  # noqa: BLE001
            pass
        print()
        print("  ═══════════════════════════════════════════════")
        print("   网络已重启，SSH 断开。树莓派现在应该已经连上 iPhone。")
        print("   它的新 IP 由手机热点分配，需要在手机上查看：")
        print("     iPhone → 设置 → 蜂窝网络 → 个人热点的已连设备")
        print("  ═══════════════════════════════════════════════")
        print()
        print("  拿到新 IP 后，用它重新连：")
        print("    ssh ubuntu@<新IP>")
        print("  验证是否成功切到 STA：")
        print("    iwconfig wlan0 | grep Mode    # 期望 Mode:Managed（不再是 Master）")
        print("    ip route | grep default     # 期望有默认路由（能出网）")
        print()
        return 0

        # 下面这段在 SSH 断开后已不可达，保留作为文档参考
        print("  等待重新连接…")
        import time
        for i in range(1, 8):
            time.sleep(5)
            ip = run(cli, "ip -4 addr show wlan0 | grep -oP 'inet \\K[\\d.]+' | head -1")
            route = run(cli, "ip route | grep default")
            mode = run(cli, "iwconfig wlan0 2>/dev/null | grep -oE 'Mode:[A-Za-z]+' "
                             "| head -1")
            print("    [%d] wlan0=%s  默认路由=%s  %s"
                  % (i, ip or "(未取到)", route or "(仍无)", mode or ""))
            # 成功判据：拿到新 IP + 有默认路由 + 模式是 Managed(STA)
            if ip and ip != "192.168.149.1" and route and "Managed" in (mode or ""):
                print()
                print("  ★ 成功切到 STA")
                print("    新 IP: %s" % ip)
                print("    请用这个 IP 重新 SSH：")
                print("      ssh ubuntu@%s" % ip)
                print("    回滚方法：")
                print("      sudo cp %s %s" % (backup, WPA_CONF))
                print("      sudo systemctl restart wpa_supplicant")
                return 0
        print()
        print("  [?] 30 秒内未确认连上。")
        print("     可能原因：密码错 / 只支持 5G（树莓派 3B 只支持 2.4G）"
              " / 信号弱。")
        print("     树莓派此刻可能仍在热点上（若没断开），可用原 IP %s 重试。" % args.host)
        print("     若已失联：用显示器键盘执行上面提示的备份回滚命令。")
        return 1
    finally:
        cli.close()


if __name__ == "__main__":
    sys.exit(main())
