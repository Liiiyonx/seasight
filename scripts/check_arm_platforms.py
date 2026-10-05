#!/usr/bin/env python3
"""机械臂自带演示平台体检（纯只读，不驱动任何关节）。

回答三个问题
------------
1. **系统资源够不够** —— 排除"树莓派性能不足导致卡顿"
2. **MoveIt 仿真平台能不能起来** —— 以及起不来的**真实原因**
3. **厂商上位机 GUI 能不能用** —— 这是不依赖 MoveIt 的替代演示路径

★ 2026-10-05 实测结论（别再重复排查）
-----------------------------------------
* **MoveIt 起不来，不是卡顿，是库版本错配**：
  核心库 22 个是 ``.so.1.0.7``，而全部 55 个 rviz 插件是 ``.so.1.0.8``。
  插件要 1.0.8 的核心库 → 磁盘只有 1.0.7 → ``move_group`` exit 127 直接死。
  修复：``sudo apt install --only-upgrade ros-melodic-moveit-core``
  （**需要出网**，当前树莓派无默认路由）。
* **roslaunch 报 "package not found" 是另一个坑**：必须 source **两个**
  setup.bash，只 source ``/opt/ros/melodic`` 会找不到工作空间里的包。
* **厂商上位机 GUI 正常可用**：``~/ArmPi_PC_Software/ArmPi.py``（不是
  ArmPiFPV_GUI.py —— 那是资料里写的名字，实机不存在），
  ``DISPLAY=:0`` 下进程存活、X11 窗口树里能看到 wrapper 窗口。

用法::

    export PI_PASSWORD=xxx
    python scripts/check_arm_platforms.py
    python scripts/check_arm_platforms.py --quick   # 只查资源，不启动任何东西
"""

import argparse
import os
import re
import sys

import paramiko

# 每次都 source 两个 setup.bash：只 source melodic 找不到工作空间里的包
ROS_SETUP = (
    "source /opt/ros/melodic/setup.bash && "
    "source /home/ubuntu/armpi_fpv/devel/setup.bash"
)

RESOURCE_CHECKS = [
    ("内存", "free -m | head -2 | tail -1"),
    ("负载", "uptime"),
    ("磁盘", "df -h / | tail -1"),
    ("显示环境", "export DISPLAY=:0; xset q >/dev/null 2>&1 && echo ':0 可用' || echo ':0 不可用'"),
    ("X11 窗口数", "export DISPLAY=:0; xwininfo -root 2>/dev/null | grep -i children || echo '?'"),
]

MOVEIT_CHECKS = [
    ("moveit 库版本分布",
     "ls /opt/ros/melodic/lib/libmoveit_*.so.* 2>/dev/null | "
     "sed 's|.*/||' | grep -oE '1\\.0\\.[0-9]+' | sort | uniq -c"),
    ("已装 moveit-core 版本",
     "dpkg -l 2>/dev/null | grep -E 'moveit-core|moveit-ros-visualization' | "
     "awk '{print $2, $3}' | head -4"),
    ("apt 可用候选版本",
     "apt-cache policy ros-melodic-moveit-core 2>/dev/null | head -3"),
    ("move_group 缺失依赖数",
     "ldd /opt/ros/melodic/lib/moveit_ros_move_group/move_group 2>/dev/null "
     "| grep -c 'not found'"),
    ("包能否被 rospack 解析",
     ROS_SETUP + " && rospack find armpi_fpv_moveit_config 2>&1 | head -1"),
]

GUI_CHECKS = [
    ("上位机文件", "ls -1 ~/ArmPi_PC_Software/ArmPi.py 2>/dev/null || echo 'NOT FOUND'"),
    ("桌面启动项", "grep -h '^Exec=' ~/Desktop/*.desktop 2>/dev/null | head -4"),
    ("NoMachine 服务", "ps aux | grep -c '[n]xserver'"),
    ("Xorg 真实会话", "ps aux | grep -c '[X]org'"),
]


def _run(cli, cmd: str, timeout: int = 30) -> str:
    _, so, se = cli.exec_command(cmd, timeout=timeout)
    so.channel.settimeout(timeout)
    out = so.read().decode("utf-8", "replace").rstrip()
    err = se.read().decode("utf-8", "replace").rstrip()
    if not out and err:
        return "(stderr) " + err
    return out


def section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def check_resources(cli) -> None:
    section("1) 系统资源 —— 排除「性能不足导致卡顿」")
    for name, cmd in RESOURCE_CHECKS:
        print("  %-24s %s" % (name, _run(cli, cmd, 20)))


def check_moveit(cli) -> None:
    section("2) MoveIt 仿真平台 —— 起不来的真实原因")
    print("  [静态检查]")
    for name, cmd in MOVEIT_CHECKS:
        print("  %-28s" % name)
        for line in _run(cli, cmd, 30).split("\n"):
            if line.strip():
                print("      " + line)

    print("\n  [启动实测 25 秒，不执行任何运动]")
    cmd = (
        "%s && export DISPLAY=:0 && "
        "export ROS_MASTER_URI=http://localhost:11311 && "
        "timeout 18 roslaunch armpi_fpv_moveit_config demo.launch 2>&1 "
        "| grep -E 'error while loading|has died|exit code|Loading robot model"
        "|cannot open shared|cannot load|RLException' | head -8"
    ) % ROS_SETUP
    out = _run(cli, cmd, 45)
    for line in out.split("\n"):
        if line.strip():
            print("      " + line)

    print("\n  判读：")
    if "cannot open shared object file" in out or "cannot load" in out:
        print("      ✗ 库版本错配 —— 核心库(1.0.7) 与 rviz 插件(1.0.8) 不一致")
        print("        修复：sudo apt install --only-upgrade ros-melodic-moveit-core")
        print("        ★ 需要出网，树莓派当前无默认路由")
    elif "RLException" in out or "not found" in out:
        print("      ✗ rospack 找不到包 —— 检查是否 source 了工作空间 setup.bash")
    elif "has died" in out or "exit code" in out:
        print("      ✗ 进程崩溃，见上方日志")
    elif not out.strip():
        print("      ✗ 无任何输出即退出 —— 多半是包解析失败（rospack 路径问题）")
    else:
        print("      ? 未匹配到已知错误模式，需人工看日志")


def check_gui(cli) -> None:
    section("3) 厂商上位机 GUI —— 不依赖 MoveIt 的替代演示路径")
    for name, cmd in GUI_CHECKS:
        print("  %-20s %s" % (name, _run(cli, cmd, 20)))

    print("\n  [启动实测 15 秒，不做任何操作]")
    cmd = (
        "%s && export DISPLAY=:0 && cd ~/ArmPi_PC_Software && "
        "timeout 12 python3 ArmPi.py >/dev/null 2>&1; "
        "echo \"exit=$?（124=存活到超时，说明正常）\""
    ) % ROS_SETUP
    print("      " + _run(cli, cmd, 40))

    print("\n  演示时用 NoMachine(4000) 连上去，打开 PC_Software 即可看到窗口。")
    print("  ⚠ 演示前记得把相机天线向后弯折（第 10 课要求）。")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=os.environ.get("PI_HOST", "192.168.149.1"))
    ap.add_argument("--user", default=os.environ.get("PI_USER", "ubuntu"))
    ap.add_argument("--quick", action="store_true",
                    help="只查资源，不启动任何进程")
    args = ap.parse_args()

    password = os.environ.get("PI_PASSWORD", "")
    if not password:
        print("需要环境变量 PI_PASSWORD（树莓派出厂口令，见厂商教程 p16）")
        return 2

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(args.host, username=args.user, password=password,
                    timeout=15, look_for_keys=False, allow_agent=False)
    except Exception as exc:  # noqa: BLE001
        print("SSH 失败：%s: %s" % (type(exc).__name__, exc))
        return 1

    print("机械臂演示平台体检 · %s · 目标 %s" % (
        __import__("time").strftime("%Y-%m-%d %H:%M:%S"), args.host))
    print("安全边界：全部只读/只启动，不驱动任何关节，不做 Plan/Execute")

    try:
        check_resources(cli)
        if not args.quick:
            check_moveit(cli)
            check_gui(cli)
    finally:
        cli.close()

    print()
    print("=" * 70)
    print("结论：MoveIt 目前不可用（库版本错配，需出网修复）；")
    print("      厂商上位机 GUI 可用，是当前唯一已验证的图形演示路径。")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
