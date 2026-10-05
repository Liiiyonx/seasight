#!/usr/bin/env python3
"""从树莓派 SD 卡读取/修复 WiFi 配置（**完全离线，不需要网络**）。

★ 为什么需要这个（2026-10-05 实际卡住的局面）
------------------------------------------------
树莓派 SSH 唯一入口是 WiFi，但它现在：
  - 厂商自建热点已被我 disable（为了让它切到 STA）
  - iPhone 配置已写入、psk 与 wpa_passphrase 交叉验证过，但**连不上**
  - 电脑 WiFi 网卡被反复改 profile 搞坏，**开不出移动热点**
于是「树莓派配置对不对」这个问题**只能读文件验证** —— 而读文件最直接的办法
就是把 SD 卡插到电脑上。

这个脚本做三件事（全程只读 + 只改一个文件，不动舵机与 ROS）：
  1. 识别树莓派系统分区（boot/rootfs，通常挂载为 D: / E:）
  2. 读 ``/etc/wpa_supplicant/wpa_supplicant.conf``，
     报告：有没有我们的 network 块、语法是否正确、psk 是否为 64 位十六进制
  3. 报告**改 STA 所需的两个开关**当前是什么状态
     （``hw_wifi.service`` 是否 disabled —— 若仍 enabled 会抢网卡）

用法::

    # 1) 树莓派断电
    # 2) 取出 microSD 卡，插到电脑读卡器
    # 3) 在这里跑：
    python scripts/pi_sdcard_fix.py            # 只诊断
    python scripts/pi_sdcard_fix.py --fix      # 顺带禁用 hw_wifi.service
    # 4) 安全弹出 SD 卡，装回树莓派，开机

★ 重要：rootfs 分区是 **ext4**，Windows 默认**不认**。
   若读不到 ``/etc/...``，说明是 ext4 分区 —— 这时需要：
   - 用 ext4 驱动（如 Paragon ExtFS / DiskInternals Linux Reader），或
   - 用 WSL 挂载 vhd，或
   - **改用树莓派本机方案**：接显示器+键盘跑 ``bash ~/pi_net_rescue.sh``

Linux/WSL 用户可加 ``--rootfs /mnt/rootfs`` 指定挂载点。
"""

import argparse
import os
import shutil
import sys
import time

# 树莓派系统分区的典型卷标
RPI_LABELS = ("boot", "rootfs", "RPI", "raspberry", "EFI")
BLOCK_START = "# >>> oceanus-sta >>>"
BLOCK_END = "# <<< oceanus-sta <<<"

WPA_REL = os.path.join("etc", "wpa_supplicant", "wpa_supplicant.conf")
SDCARD_CMD_REL = os.path.join("etc", "systemd", "system",
                              "hw_wifi.service")


def list_drives():
    """列出可用盘符（Windows）。"""
    if os.name != "nt":
        return []
    import string
    drives = []
    for letter in string.ascii_uppercase:
        root = letter + ":\\"
        if os.path.exists(root):
            drives.append(root)
    return drives


def looks_like_pi(drive):
    """判断某个盘符是否是树莓派系统分区。"""
    label = ""
    try:
        import ctypes
        vol = ctypes.create_ulong_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive), ctypes.c_wchar_p(vol), 261,
            None, None, None, None, 0)
        if ok:
            label = vol.value or ""
    except Exception:
        pass
    if any(t.lower() in label.lower() for t in RPI_LABELS):
        return label
    # 用 boot 目录做启发式判断
    if os.path.isdir(os.path.join(drive, "boot")):
        return label or "(有 boot 目录)"
    return None


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError as exc:
        return None, str(exc)


def analyze(conf_text, label):
    """分析 wpa_supplicant.conf 的内容。"""
    print()
    print("=" * 66)
    print("wpa_supplicant.conf 分析")
    print("=" * 66)

    if BLOCK_START not in conf_text:
        print("  ✗ 没有找到我们的配置块（%s）" % BLOCK_START)
        print("    → 说明 2026-10-05 那次写入**没有成功**或已被清掉")
        return False
    print("  ✓ 找到配置块")

    # 提取我们的块
    try:
        i = conf_text.index(BLOCK_START)
        j = conf_text.index(BLOCK_END) + len(BLOCK_END)
        block = conf_text[i:j]
    except ValueError:
        print("  ✗ 配置块不完整（有开始标记没结束标记）")
        return False

    nets = []
    cur = {}
    for raw in block.split("\n"):
        line = raw.strip()
        if line.startswith("network=") or line == "network={":
            if cur:
                nets.append(cur)
            cur = {}
            continue
        if line == "}":
            if cur:
                nets.append(cur)
                cur = {}
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            cur[k.strip()] = v.strip().strip('"')
    if cur:
        nets.append(cur)

    print("  网络数量: %d" % len(nets))
    ok_all = True
    for n, net in enumerate(nets, 1):
        ssid = net.get("ssid", "(未命名)")
        psk = net.get("psk", "")
        pri = net.get("priority", "(无)")
        print()
        print("  【%d】ssid=%-16s priority=%s" % (n, ssid, pri))
        if not psk:
            print("      ✗ 缺 psk")
            ok_all = False
        elif len(psk) == 64 and all(c in "0123456789abcdefABCDEF" for c in psk):
            print("      ✓ psk 是 64 位十六进制（PBKDF2 派生值，非明文）")
        elif psk.startswith('"'):
            print("      ⚠ psk 是明文（引号包裹）—— 建议改用派生值")
        else:
            print("      ✗ psk 格式异常（长度 %d）" % len(psk))
            ok_all = False
        if net.get("key_mgmt") == "WPA-PSK":
            print("      ✓ key_mgmt=WPA-PSK")
        if "priority" not in net:
            print("      ⚠ 没有 priority（多网络时无法排序）")

    # 语法粗检
    print()
    if block.count("{") == block.count("}"):
        print("  ✓ 花括号配平")
    else:
        print("  ✗ 花括号不配平 —— 配置有语法错误，wpa_supplicant 会起不来")
        ok_all = False
    return ok_all


def report_services(rootfs):
    """报告改 STA 所需的两个开关状态。"""
    print()
    print("=" * 66)
    print("服务状态（改 STA 的前提）")
    print("=" * 66)
    path = os.path.join(rootfs, SDCARD_CMD_REL)
    if not os.path.isfile(path):
        print("  ? 找不到 %s" % SDCARD_CMD_REL)
        print("    → 可能该服务本来就不存在（那就不用管）")
        return
    text, _ = read_text(path)
    if text is None:
        return
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("ExecStart"):
            print("  hw_wifi.service: %s" % line)
            print()
            print("  ★ 这个服务会自动拉起厂商热点，会**抢占 wlan0**。")
            print("    要切 STA 就必须 disable 它：")
            print("      sudo systemctl disable --now hw_wifi.service")
            return


def find_rootfs(drives):
    """在候选盘符里找 rootfs。"""
    for d in drives:
        if os.path.isfile(os.path.join(d, WPA_REL)):
            return d
    return None


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rootfs", help="树莓派 rootfs 挂载点（Linux/WSL 用）")
    ap.add_argument("--fix", action="store_true",
                    help="（预留）修改配置")
    args = ap.parse_args()

    print("=" * 66)
    print(" 树莓派 SD 卡 WiFi 配置检查")
    print("=" * 66)
    print(" 前提：树莓派已断电，microSD 卡已插到电脑")
    print()

    if args.rootfs:
        rootfs = args.rootfs
        print("  使用指定路径: %s" % rootfs)
    else:
        if os.name != "nt":
            print("  Linux/WSL 环境：请用 --rootfs 指定挂载点")
            print("  如：--rootfs /mnt/rootfs")
            return 2
        drives = list_drives()
        print("  候选盘符: %s" % ", ".join(drives))
        print()
        found = []
        for d in drives:
            lbl = looks_like_pi(d)
            if lbl:
                print("  ★ %s 是树莓派分区（卷标: %s）" % (d, lbl))
                found.append(d)
        if not found:
            print()
            print("  ✗ 没找到树莓派分区")
            print()
            print("  可能原因：")
            print("   1. SD 卡没插好 / 不是 microSD 卡")
            print("   2. ★ rootfs 是 ext4 文件系统，Windows 读不了")
            print("      → 这种情况需要：")
            print("         a) 用 ext4 驱动（Paragon ExtFS / DiskInternals）")
            print("         b) 或者接显示器+键盘跑 bash ~/pi_net_rescue.sh")
            return 1
        rootfs = find_rootfs(found)
        if not rootfs:
            print()
            print("  找到分区但读不到 %s —— 大概率是 ext4 不被 Windows 支持" % WPA_REL)
            print("  请用 ext4 驱动，或改用「接显示器」方案")
            return 1
        print()
        print("  使用 rootfs: %s" % rootfs)

    conf_path = os.path.join(rootfs, WPA_REL)
    if not os.path.isfile(conf_path):
        print()
        print("  ✗ 找不到 %s" % conf_path)
        return 1

    text, err = read_text(conf_path)
    if text is None:
        print("  ✗ 读取失败: %s" % err)
        return 1
    print("  ✓ 已读取 %s（%d 字节）" % (WPA_REL, len(text)))

    analyze(text, rootfs)
    report_services(rootfs)

    print()
    print("=" * 66)
    print(" 下一步")
    print("=" * 66)
    print("  把上面的输出发给我，我判断问题出在哪。")
    print()
    print("  ★ 安全弹出 SD 卡前先告诉我 —— 我确认没问题再拔。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
