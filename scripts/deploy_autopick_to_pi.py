#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把「一键自动抓取」部署包打到树莓派上（在 Windows 开发机上运行）。

## 这个脚本补的是哪一段

`edge/grasp/deploy/run_autopick.sh` 解决的是**板子上**「开机到机械臂自己捡」
之间的所有步骤。但在此之前还有一段手工活：打包哪些文件、拷到哪、装什么依赖、
跑哪个 install.sh、怎么在**不接通电源**的前提下确认链路是好的。

以前这段是文档里的一串手工命令（见 `项目文档/探海灵眸_ArmPi真机部署清单.md`），
手抄容易漏项 —— 尤其是漏模型（`models/active/best.onnx` 在 .gitignore 里，
不从开发机带过去的话板上自检直接 `✗ 模型`）和漏可执行位。

本脚本把这段固化成一条命令：

    python scripts/deploy_autopick_to_pi.py --host 192.168.149.1
    python scripts/deploy_autopick_to_pi.py --host 192.168.149.1 --apply

## 安全边界（与 deploy_board_agent.py / deploy_bridge_to_pi.py 同一口径）

* 默认**只做验证**：拷代码 + 提单条 `--check`（自检）/ `--mock --once`（离线闭环）。
  这两条**都不驱动舵机、不发任何运动指令**。
* 要真的让机械臂动起来必须显式加 `--apply` —— 加了它才会去
  `systemctl enable --now oceanus-arm-autopick`。首次真机动作应当**有人在场**，
  且工作区内无遮挡。

## 前提

* 开发机能 `ssh` 上板（走系统 OpenSSH 的 `ssh` / `scp`，不需要 paramiko）。
* 板上已有厂商 ROS 工作空间（`--ros-setup`，默认 `~/armpi_fpv/devel/setup.bash`）。
* 首次使用建议先跑一次 `--dry-run` 看它打算拷哪些文件。
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import posixpath
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EDGE = REPO / "edge"
GRASP = EDGE / "grasp"
DEPLOY = GRASP / "deploy"

#: 打进部署包的路径（相对仓库根），目录会被递归收进去。
#: 只收运行时真正需要的：supervisor 闭环 + 视觉 + 工具 + 部署脚本 + 模型。
#: ★ 注意没有 `edge/__init__.py` —— `edge/` 不是一个 Python 包，
#:   它是被加进 sys.path 的**路径根**（supervisor.py 里 `EDGE = HERE.parent`），
#:   所以板上按 `python3 -m grasp.supervisor` 从 edge/ 目录下运行。
INCLUDE = [
    "edge/grasp/__init__.py",
    "edge/grasp/arm_io.py",
    "edge/grasp/calibration.py",
    "edge/grasp/config.py",
    "edge/grasp/config.yaml",
    "edge/grasp/grasp_loop.py",
    "edge/grasp/supervisor.py",
    "edge/grasp/vision_driver.py",
    "edge/grasp/requirements.txt",
    "edge/grasp/requirements-runtime.txt",
    "edge/grasp/vision",
    "edge/grasp/tools",
    "edge/grasp/deploy",
]
#: 只有走作业台（采集/标定/示教/训练）时才需要的路径
CONSOLE_EXTRA = ["edge/grasp/web"]
#: 只有走平台派单模式时才需要的路径
DISPATCH_EXTRA = ["edge/arm_bridge"]

#: 永远不进包的东西
#: ★ "data" / "runs" 是按**路径段**精确匹配的（见 _excluded），不是子串匹配 ——
#:   否则会误伤 vision/dataset.py 这类名字里带 data 的源码。
EXCLUDE_PATTERNS = [
    "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".git",
    "data", "runs", "models/versions",
    # 开发机上的探针脚本（_probe_*.py）：靠固定 IP / 本机私有环境跑，
    # 上板只会让现场误以为「有现成工具」，增加排障噪音
    "_probe_*.py",
]

#: 模型（38MB，且在 .gitignore 里）：不带上板的话自检会明确 ✗ 模型。
#: 单独列出是因为它要能被 --no-model 单独摘掉（板上已有模型时省一次 38MB 传输）。
MODEL_FILES = ["edge/grasp/models/active/best.onnx", "edge/grasp/models/index.json"]

#: autopick 运行时的最小依赖（ONNX 后端只走 cv2.dnn，**不需要 ultralytics**）
MINIMAL_DEPS = ["numpy", "PyYAML", "opencv-python"]


def log(msg: str) -> None:
    print(f"[deploy] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[deploy] ⚠ {msg}", file=sys.stderr, flush=True)


def die(msg: str, code: int = 2) -> "None":
    print(f"[deploy] ✗ {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


# ======================================================================
# 打包
# ======================================================================
def _excluded(rel: str) -> bool:
    parts = rel.split("/")
    for pat in EXCLUDE_PATTERNS:
        if any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
        if fnmatch.fnmatch(rel, pat):
            return True
    return False


def _collect(rel: str, want_model: bool, with_console: bool, with_dispatch: bool) -> list[tuple[Path, str]]:
    """把 INCLUDE 展开成 (本地绝对路径, 包内相对路径) 列表。"""
    roots = list(INCLUDE)
    if with_console:
        roots += CONSOLE_EXTRA
    if with_dispatch:
        roots += DISPATCH_EXTRA
    if want_model:
        roots += MODEL_FILES

    out: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for rel in roots:
        src = REPO / rel
        if src.is_file():
            if rel not in seen and not _excluded(rel):
                seen.add(rel)
                out.append((src, rel))
            continue
        if not src.is_dir():
            warn(f"跳过（不存在）：{rel}")
            continue
        for path in sorted(src.rglob("*")):
            if not path.is_file():
                continue
            sub = path.relative_to(REPO).as_posix()
            if _excluded(sub) or sub in seen:
                continue
            seen.add(sub)
            out.append((path, sub))
    return out


def build_tarball(args: argparse.Namespace, files: list[tuple[Path, str]], dest: Path) -> int:
    """打 tar.gz；保留可执行位（run_autopick.sh / install.sh 靠它）。"""
    total = 0
    with tarfile.open(dest, "w:gz") as tf:
        for src, rel in files:
            info = tf.gettarinfo(str(src), arcname=rel)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            # 让 .sh 在板上直接可执行；install.sh 里也会再 chmod 一次兜底
            if rel.endswith(".sh"):
                info.mode = 0o755
            else:
                info.mode = 0o644
            with open(src, "rb") as fh:
                tf.addfile(info, fh)
            total += info.size
    return total


# ======================================================================
# 远端执行
# ======================================================================
def remote(args: argparse.Namespace) -> str:
    return f"{args.user}@{args.host}"


def ssh_base(args: argparse.Namespace) -> list[str]:
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if args.port != 22:
        cmd += ["-p", str(args.port)]
    return cmd


def scp_base(args: argparse.Namespace) -> list[str]:
    cmd = ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if args.port != 22:
        cmd += ["-P", str(args.port)]
    return cmd


def run(cmd: list[str], *, dry: bool, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    shown = " ".join(shlex.quote(c) for c in cmd)
    log(f"$ {shown}")
    if dry:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    # 子进程也强制 UTF-8：本脚本输出带中文与 ✓/✗，在 GBK 控制台上
    # 子进程 print 会 UnicodeEncodeError（自检表就是这样被打断的）
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if capture and proc.stdout:
        for line in proc.stdout.rstrip().splitlines():
            print(f"    {line}")
    if capture and proc.stderr.strip():
        for line in proc.stderr.rstrip().splitlines():
            print(f"    ! {line}", file=sys.stderr)
    if check and proc.returncode != 0:
        # 统一成 2（前置/环境不满足），而不是把 ssh 的 255 直接透出去 ——
        # 调用方与 CI 只看「非 0 = 没部署成功」，但人看到 2 能对上本脚本的约定
        detail = f"（{proc.stderr.strip().splitlines()[-1]}" if (proc.stderr or "").strip() else ""
        detail = detail + "）" if detail else ""
        die(f"命令失败（退出码 {proc.returncode}）：{shown}\n    {detail}", 2)
    return proc


def run_remote(args: argparse.Namespace, script: str, *, dry: bool, check: bool = True) -> subprocess.CompletedProcess:
    """在板上跑一段 bash。用 shlex.quote 整体转义，避免引号地狱。"""
    return run(ssh_base(args) + [remote(args), f"bash -lc {shlex.quote(script)}"],
               dry=dry, check=check)


# ======================================================================
# 板上收尾脚本
# ======================================================================
#: 这段在板上跑。为什么要写成文件而不是 ssh 一行命令：
#: install.sh 的模板替换用到 __REPO__ 这类占位符，多层引号在 Windows 上
#: 极容易转义错，落成脚本可读可复查（也便于现场手工重跑）。
BOARD_FINISH = r"""#!/usr/bin/env bash
# 由 scripts/deploy_autopick_to_pi.py 上传；板上解包 + 装依赖 + 装 systemd 单元。
set -uo pipefail

TARBALL="$1"; REPO="$2"; ROS_SETUP="$3"; BROKER="$4"; MODE="$5"
WITH_CONSOLE="$6"; SKIP_DEPS="$7"; SKIP_SYSTEMD="$8"; ROS_MASTER="$9"; ROS_IP="${10}"

# 空的 ROS_IP 交给 install.sh 自己按 hostname -I 推断：从 Windows 侧传一个
# 空参数要穿过 bash -lc + 单引号两层引用，很容易变成一个字面量空串参数；
# 在这里兜底比在命令行里跟引号较劲更可靠。
[[ -n "$ROS_IP" ]] || ROS_IP="__AUTO__"

log() { echo "[board] $*"; }
warn() { echo "[board] ! $*" >&2; }

EDGE="$REPO/edge"
mkdir -p "$REPO"
log "解包 $TARBALL → $REPO"
tar -xzf "$TARBALL" -C "$REPO"
chmod +x "$REPO/edge/grasp/deploy/run_autopick.sh" 2>/dev/null || true
chmod +x "$REPO/edge/grasp/deploy/systemd/install.sh" 2>/dev/null || true
log "解包完成：$(find "$EDGE" -type f | wc -l) 个文件"

# 行尾自愈：Windows 侧万一漏了 .gitattributes，CRLF 会让 bash 报
# "bad interpreter: No such file or directory"，现场极难看出原因。
for f in "$EDGE/grasp/deploy/run_autopick.sh" "$EDGE/grasp/deploy/systemd/install.sh"; do
  if [[ -f "$f" ]] && grep -q $'\r' "$f" 2>/dev/null; then
    warn "$(basename "$f") 是 CRLF 行尾，自动转换成 LF"
    sed -i 's/\r$//' "$f"
  fi
done

if [[ "$SKIP_DEPS" != "1" ]]; then
  log "安装 Python 依赖（推理链路只需 numpy / PyYAML / opencv，**不需要 ultralytics**）"
  PYV="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo '?')"
  log "板上 python3 版本：$PYV（作业台/桥接层需要 >=3.7；纯 autopick 推理不强制）"

  # 先装最小集 —— 这一份装不上，自动抓取就跑不了，必须报清楚
  if [[ -f "$EDGE/grasp/requirements-runtime.txt" ]]; then
    python3 -m pip install --user -r "$EDGE/grasp/requirements-runtime.txt" \
      || warn "最小依赖没装全 —— 板上试 apt：sudo apt install -y python3-opencv python3-numpy python3-yaml"
  fi

  # 再尝试完整集。装不上**不算部署失败**：uvicorn/ultralytics 只有作业台
  # 训练档用得到，autopick 用不到。
  if [[ -f "$EDGE/grasp/requirements.txt" ]]; then
    python3 -m pip install --user -r "$EDGE/grasp/requirements.txt" \
      || warn "完整 requirements.txt 未全部装成功（多半是 ultralytics/uvicorn）—— 不影响自动抓取；要作业台再单独补"
  fi
else
  log "跳过依赖安装（--skip-deps）"
fi

# 关键依赖可用性直接验一遍：装了但 import 不进来才是最坑的（版本冲突）
if [[ "$SKIP_DEPS" != "1" ]]; then
  python3 - <<'PYEOF' || warn "关键依赖 import 失败 —— 自动抓取起不来，按上面的报错补装"
import sys
missing = []
for mod in ("cv2", "numpy", "yaml"):
    try:
        __import__(mod)
    except Exception as exc:      # noqa: BLE001
        missing.append("%s (%s)" % (mod, exc))
if missing:
    print("[board] ! 缺关键依赖: %s" % ", ".join(missing))
    sys.exit(1)
print("[board] 关键依赖 OK: cv2 / numpy / yaml")
PYEOF
fi

# 模型自检：模型在 .gitignore 里，最容易被漏掉的一项
if [[ -f "$EDGE/grasp/models/active/best.onnx" ]]; then
  log "模型就位：$(du -h "$EDGE/grasp/models/active/best.onnx" | cut -f1) best.onnx"
else
  warn "models/active/best.onnx 不在板上 —— 自检会 ✗ 模型。"
  warn "  带模型重跑部署，或在开发机跑：python scripts/deploy_autopick_to_pi.py --host <IP> --only-model"
fi

if [[ "$SKIP_SYSTEMD" != "1" ]]; then
  log "安装 systemd 单元（mode=$MODE, ros-master=$ROS_MASTER）"
  sudo bash "$EDGE/grasp/deploy/systemd/install.sh" \
      --repo "$REPO" --broker "$BROKER" --mode "$MODE" --ros-setup "$ROS_SETUP" \
      --ros-master "$ROS_MASTER" --ros-ip "$ROS_IP" \
    || { warn "install.sh 失败 —— 手动跑：sudo bash $EDGE/grasp/deploy/systemd/install.sh --repo $REPO --broker $BROKER --mode $MODE --ros-setup $ROS_SETUP"; exit 3; }
else
  log "跳过 systemd 安装（--skip-systemd）"
fi
log "板上收尾完成"
"""


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        description="把一键自动抓取部署到树莓派（默认只验证，不动机械臂）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/deploy_autopick_to_pi.py --host 192.168.149.1 --dry-run\n"
            "  python scripts/deploy_autopick_to_pi.py --host 192.168.149.1\n"
            "  python scripts/deploy_autopick_to_pi.py --host 192.168.149.1 --apply\n"
        ),
    )
    ap.add_argument("--host", required=True, help="树莓派地址（如 192.168.149.1）")
    ap.add_argument("--user", default="ubuntu", help="板上登录用户（默认 ubuntu；老镜像常是 pi）")
    ap.add_argument("--port", type=int, default=22, help="SSH 端口")
    ap.add_argument("--remote-repo", default=None,
                    help="板上仓库根目录（默认 /home/<user>/oceanus）")
    ap.add_argument("--ros-setup", default=None,
                    help="厂商工作空间 setup.bash（默认 ~/armpi_fpv/devel/setup.bash）")
    ap.add_argument("--ros-master", default="http://127.0.0.1:11311",
                    help="ROS master 地址（默认板子本机；master 在别的机器时改这里）")
    ap.add_argument("--ros-ip", default="",
                    help="板子的 ROS_IP（默认由板上 hostname -I 自动取第一个地址）")
    ap.add_argument("--broker", default="127.0.0.1",
                    help="平台那侧 MQTT broker 地址（走作业台/派单才用得上）")

    ap.add_argument("--mode", default="autopick", choices=["autopick", "dispatch", "console", "all"],
                    help="启用哪套 systemd 单元（默认 autopick=常驻自捡）")
    ap.add_argument("--with-console", action="store_true",
                    help="同时带上作业台 web/（采集/标注/训练/标定/示教要用）")
    ap.add_argument("--with-dispatch", action="store_true",
                    help="同时带上 arm_bridge/（平台派单模式用）")
    ap.add_argument("--no-model", action="store_true",
                    help="不带 models/active（板上已有模型时省 38MB）")
    ap.add_argument("--only-model", action="store_true",
                    help="只补模型，不碰代码与 systemd")

    ap.add_argument("--skip-deps", action="store_true", help="不在板上装 pip 依赖")
    ap.add_argument("--skip-systemd", action="store_true", help="只解包，不装 systemd 单元")
    ap.add_argument("--apply", action="store_true",
                    help="★ 启用并启动 oceanus-arm-autopick（会让机械臂真的动）")
    ap.add_argument("--no-verify", action="store_true", help="跳过部署后的 --check/--mock 验证")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令")

    args = ap.parse_args(argv)
    args.remote_repo = args.remote_repo or f"/home/{args.user}/oceanus"
    args.ros_setup = args.ros_setup or f"/home/{args.user}/armpi_fpv/devel/setup.bash"

    for tool in ("ssh", "scp"):
        from shutil import which
        if which(tool) is None:
            die(f"找不到 {tool} —— Windows 上装「OpenSSH 客户端」功能，或把 Git 的 usr/bin 加进 PATH")

    files = _collect(
        ".",
        want_model=not args.no_model or args.only_model,
        with_console=args.with_console or args.mode in ("console", "all") or args.only_model,
        with_dispatch=args.with_dispatch or args.mode in ("dispatch", "all"),
    )
    if not files:
        die("没有要部署的文件 —— 检查仓库根目录是否正确")
    if args.only_model:
        files = [(p, r) for p, r in files if r.startswith("edge/grasp/models/")]

    print()
    log(f"目标：{remote(args)}:{args.remote_repo}")
    log(f"模式：{args.mode}   带模型：{not args.no_model or args.only_model}   "
        f"文件数：{len(files)}")
    if args.dry_run:
        log("--dry-run：只列文件与命令，不做任何改动")
    for p, r in files[:8]:
        print(f"    {r}  ({p.stat().st_size / 1024:.0f} KB)")
    if len(files) > 8:
        print(f"    … 其余 {len(files) - 8} 个")

    with tempfile.TemporaryDirectory(prefix="oceanus-deploy-") as tmp:
        tmpdir = Path(tmp)
        tarball = tmpdir / "oceanus_edge.tar.gz"
        size = build_tarball(args, files, tarball)
        log(f"打包完成：{tarball.name}  {size / 1024 / 1024:.1f} MB")

        # 连通性：先确认能 SSH，再传东西（失败时报错更清楚）。
        # 这里失败几乎总是「IP 不对 / 板子没起来 / 没配免密」，所以单独给一句可执行的提示。
        result = run_remote(args, "echo board-ok && hostname && python3 --version",
                            dry=args.dry_run, check=False)
        if not args.dry_run and result.returncode != 0:
            die(
                f"连不上 {remote(args)}（ssh 退出码 {result.returncode}）。\n"
                f"    先手工确认：ssh {remote(args)} 'hostname'\n"
                f"    常见原因：IP 不对 / 板子没开机 / 没配免密登录（BatchMode 下不会交互问密码）",
                2,
            )

        run_remote(args, f"mkdir -p {shlex.quote(args.remote_repo)}", dry=args.dry_run)

        remote_tar = posixpath.join(args.remote_repo, tarball.name)
        run(scp_base(args) + [str(tarball), f"{remote(args)}:{remote_tar}"], dry=args.dry_run)

        # 板上收尾脚本
        finish_local = tmpdir / "board_finish.sh"
        finish_local.write_text(BOARD_FINISH, encoding="utf-8", newline="\n")
        finish_remote = posixpath.join(args.remote_repo, "board_finish.sh")
        run(scp_base(args) + [str(finish_local), f"{remote(args)}:{finish_remote}"], dry=args.dry_run)

        run_remote(
            args,
            f"bash {shlex.quote(finish_remote)} {shlex.quote(remote_tar)} "
            f"{shlex.quote(args.remote_repo)} {shlex.quote(args.ros_setup)} "
            f"{shlex.quote(args.broker)} {shlex.quote(args.mode)} "
            f"{'1' if (args.with_console or args.mode in ('console', 'all')) else '0'} "
            f"{'1' if args.skip_deps else '0'} "
            f"{'1' if (args.skip_systemd or args.only_model) else '0'} "
            f"{shlex.quote(args.ros_master)} "
            # ★ 空字符串不要在命令行里传：它要穿过 bash -lc + 单引号两层引用，
            #   会退化成 `''` 这样的字面量空参数甚至把整行引号拆散（实测过）。
            #   用一个哨兵值代替，由 install.sh 解释成「自己按 hostname -I 推断」。
            f"{shlex.quote(args.ros_ip or '__AUTO__')}",
            dry=args.dry_run,
        )
        run_remote(args, f"rm -f {shlex.quote(remote_tar)} {shlex.quote(finish_remote)}", dry=args.dry_run)

        # ---------------- 验证：都不驱动舵机 ----------------
        if not args.no_verify:
            print()
            log("验证 1/2：板上离线闭环（--mock --once，合成画面 + 模拟舵机，不碰 ROS）")
            run_remote(
                args,
                f"cd {shlex.quote(args.remote_repo)} && "
                f"bash edge/grasp/deploy/run_autopick.sh --mock --once",
                dry=args.dry_run, check=False,
            )
            print()
            log("验证 2/2：真机前置自检（--check，只读，不动机械臂）")
            run_remote(
                args,
                f"cd {shlex.quote(args.remote_repo)} && "
                f"bash edge/grasp/deploy/run_autopick.sh --check --ros-setup {shlex.quote(args.ros_setup)}",
                dry=args.dry_run, check=False,
            )

        # ---------------- 真的让它跑 ----------------
        print()
        if args.apply:
            log("★ --apply：启用并启动常驻自捡（机械臂会开始运动，确认工作区无遮挡）")
            run_remote(args, "sudo systemctl enable --now oceanus-arm-autopick", dry=args.dry_run, check=False)
            time.sleep(3)
            run_remote(args, "systemctl --no-pager --full status oceanus-arm-autopick | head -30",
                       dry=args.dry_run, check=False)
            log("看实时作业日志：ssh " + remote(args) + " 'journalctl -u oceanus-arm-autopick -f'")
        else:
            log("未加 --apply，没有启动任何会动机械臂的服务。")
            log("确认上面两项验证通过后，启用常驻自捡：")
            log(f"  ssh {remote(args)} 'sudo systemctl enable --now oceanus-arm-autopick'")
            log("或直接前台跑一次看全部输出：")
            log(f"  ssh {remote(args)} 'cd {args.remote_repo} && bash edge/grasp/deploy/run_autopick.sh'")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
