#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""部署包自检：在**开发机上**模拟板上的解包与启动流程。

## 这个脚本验的是什么

`scripts/deploy_autopick_to_pi.py` 把一堆文件打成 tar.gz 送到树莓派上。
打包这一段在开发机上完全可测，而它恰恰有几个**只在真机上才暴露**的坑：

1. **文件漏了** —— 例如模型 `models/active/best.onnx`（在 .gitignore 里）、
   或 `requirements-runtime.txt`。漏了要到板上跑 `--check` 才发现。
2. **行尾变成 CRLF** —— 代码用 tar 传，绕过了 git 的 `.gitattributes`，
   但开发机上的工作区副本本身可能就是 CRLF。板上 bash 会报
   `bad interpreter: No such file or directory`，看着完全莫名其妙。
3. **可执行位丢了** —— `run_autopick.sh` 没有 +x，直接跑会被拒。

本脚本把这三件事都在开发机上验掉，并且**真的解包 + 真的跑一遍
`run_autopick.sh --mock --once`**（合成画面 + 模拟舵机，不碰 ROS、不驱动舵机）。

## 用法

    python scripts/verify_autopick_package.py
    python scripts/verify_autopick_package.py --keep    # 保留临时目录便于翻看

退出码：全部通过 0，任一失败 1。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import deploy_autopick_to_pi as d  # noqa: E402

#: 板上必备文件（漏一个都会让「一键自动抓取」起不来或空转）
REQUIRED = [
    "edge/grasp/__init__.py",
    "edge/grasp/supervisor.py",
    "edge/grasp/grasp_loop.py",
    "edge/grasp/arm_io.py",
    "edge/grasp/calibration.py",
    "edge/grasp/config.py",
    "edge/grasp/config.yaml",
    "edge/grasp/vision/detector.py",
    "edge/grasp/tools/precheck.py",
    "edge/grasp/deploy/run_autopick.sh",
    "edge/grasp/deploy/systemd/install.sh",
    "edge/grasp/deploy/systemd/oceanus-arm-autopick.service",
    "edge/grasp/requirements-runtime.txt",
    "edge/grasp/models/active/best.onnx",
]

#: 不该出现在包里的东西
FORBIDDEN_SUBSTR = ["__pycache__", "/versions/", ".pyc", "_probe_", "/data/", "/runs/"]


def find_bash() -> str | None:
    """找一个**真能跑**的 bash，并且排除 WSL 的转发壳。

    ★ 这里踩过一个真实的坑：`C:\\Windows\\System32\\bash.exe` 是 WSL 的转发器，
    它本身不是 bash。如果一个非交互进程（比如本脚本）去执行它，而 WSL 里没有
    发行版 / 没装 bash，就会报
        WSL (xxxx - Relay) ERROR: execvpe(/bin/bash) failed
    现象是**命令返回非 0、且 stdout 一个字都没有** —— 极难从输出看出原因。
    所以：先试 Git Bash 的固定安装位置，再退回 PATH（但跳过 System32）。
    """
    candidates: list[str] = []
    for p in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"),
        os.path.expandvars(r"%ProgramFiles%\Git\usr\bin\bash.exe"),
    ):
        if p and Path(p).exists():
            candidates.append(p)
    which = shutil.which("bash")
    if which and "system32" not in which.lower():
        candidates.append(which)

    for cand in candidates:
        try:
            probe = subprocess.run([cand, "-c", "echo bash-ok"],
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0 and "bash-ok" in (probe.stdout or ""):
            return cand
    return None


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="部署包自检（开发机上模拟板上流程）")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    ap.add_argument("--skip-run", action="store_true",
                    help="只验包内容，不跑 run_autopick.sh（没有 bash 时用）")
    args = ap.parse_args(argv)

    failures: list[str] = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        mark = "✓" if ok else "✗"
        print(f"  [{mark}] {label}" + (f" —— {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    print("\n部署包自检")
    print("─" * 62)

    files = d._collect(".", want_model=True, with_console=False, with_dispatch=False)
    names = sorted(r for _, r in files)
    print(f"  打包 {len(files)} 个文件")

    # 1) 必备文件在不在
    missing = [r for r in REQUIRED if r not in names]
    check(not missing, "必备文件齐全", "缺：" + ", ".join(missing) if missing else f"{len(REQUIRED)} 项")

    # 2) 垃圾文件有没有混进去
    dirty = [n for n in names if any(s in n for s in FORBIDDEN_SUBSTR)]
    check(not dirty, "无 pycache / 训练产物 / 探针脚本", ", ".join(dirty[:5]) if dirty else "")

    # 3) 真的打一次包，看内容与权限位
    work = Path(tempfile.mkdtemp(prefix="oceanus-verify-"))
    try:
        tarball = work / "oceanus_edge.tar.gz"
        size = d.build_tarball(argparse.Namespace(), files, tarball)
        print(f"  包大小 {size / 1024 / 1024:.1f} MB")
        with tarfile.open(tarball) as tf:
            members = {m.name: m for m in tf.getmembers()}

        check("edge/grasp/models/active/best.onnx" in members, "模型已打入包中")

        run_m = members.get("edge/grasp/deploy/run_autopick.sh")
        check(bool(run_m) and run_m.mode & 0o111 != 0, "run_autopick.sh 带可执行位",
              oct(run_m.mode) if run_m else "缺失")

        # 4) 模拟板上解包
        remote_repo = work / "oceanus"
        remote_repo.mkdir(parents=True)
        with tarfile.open(tarball) as tf:
            try:
                tf.extractall(remote_repo, filter="data")   # py3.12+
            except TypeError:
                tf.extractall(remote_repo)
        extracted = remote_repo / "edge" / "grasp" / "deploy" / "run_autopick.sh"
        check(extracted.is_file(), "解包后启动脚本就位")
        raw = extracted.read_bytes()
        check(b"\r" not in raw, "解包后是 LF 行尾", "含 CR，板上会 bad interpreter" if b"\r" in raw else "")

        # 5) CRLF 自愈路径：board_finish.sh 的 sed 必须能修回来
        bash = find_bash()
        if bash:
            print(f"  使用 bash：{bash}")
            extracted.write_bytes(raw.replace(b"\n", b"\r\n"))
            fix = subprocess.run([bash, "-c", f"sed -i 's/\\r$//' {extracted.as_posix()}"],
                                 capture_output=True, text=True, errors="replace")
            still_cr = b"\r" in extracted.read_bytes()
            check(not still_cr, "CRLF 自愈逻辑有效",
                  f"sed rc={fix.returncode} err={fix.stderr.strip()[:120]}" if still_cr else "")
        else:
            print("  [–] 找不到 bash，跳过行尾自愈与端到端运行验证")

        # 6) 端到端：从解包目录真的跑一轮闭环（mock，不碰 ROS）
        if bash and not args.skip_run:
            py = REPO / ".venv-analysis" / "Scripts" / "python.exe"
            if not py.exists():
                py = Path(sys.executable)
            env = dict(os.environ, OCEANUS_PY=py.as_posix(),
                       OCEANUS_LOG_DIR=(work / "logs").as_posix(),
                       PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            print("  跑 run_autopick.sh --mock --once（合成画面，模拟舵机）")
            # 用 `cd <dir> && bash <相对路径>`：路径全部保持相对，
            # 避免 Git Bash 的 MSYS 路径转换把 /c/... 改写成 C:\...（踩过：
            # 转换成 C:\tmp\... 导致 precheck.py 找不到）
            script = (f"cd {work.as_posix()} && bash oceanus/edge/grasp/deploy/"
                      f"run_autopick.sh --mock --once")
            proc = subprocess.run(
                [bash, "-lc", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
            )
            out = proc.stdout or ""
            check(proc.returncode == 0, "run_autopick.sh --mock --once 退出码 0",
                  f"实际 {proc.returncode}")
            check("抓取成功" in out, "闭环跑到「抓取成功」")
            if proc.returncode != 0 or "抓取成功" not in out:
                print("    --- stdout 末尾 ---")
                for line in out.strip().splitlines()[-15:]:
                    print("   ", line)
                err = (proc.stderr or "").strip()
                if err:
                    print("    --- stderr 末尾 ---")
                    for line in err.splitlines()[-15:]:
                        print("   ", line)

        print("─" * 62)
        if failures:
            print(f"未通过 {len(failures)} 项：{'、'.join(failures)}")
            return 1
        print("部署包自检全部通过 —— 可以直接 "
              "python scripts/deploy_autopick_to_pi.py --host <板子IP> 真部署。")
        print("注：这只证明包是完整可跑的，**不代表真机能抓**（真机精度需真机验证，口径 E2）。")
        return 0
    finally:
        if args.keep:
            print(f"[verify] 临时目录保留：{work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
