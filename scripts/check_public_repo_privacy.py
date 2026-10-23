"""公开仓库发布前的隐私/脱敏扫描。

用于华为 ICT 入围总决赛前的 GitCode/GitHub 开源准备。只扫描 git 已跟踪
文件，不碰工作区里未跟踪的本地草稿；命中以下内容时必须处理：

1. 本机绝对路径（用户名、Desktop、AppData 等）；
2. 生产服务器地址（部署时另行配置，不随公开代码出现）；
3. 已知包含个人/环境信息的验收证据目录或脚本。

私有开发仓库保留验收证据而命中扫描结果是预期行为，不代表功能失败；
公开动作前该命令必须退出码 0。

用法：
    # 扫描 git 已跟踪文件（私有开发仓库）
    python scripts/check_public_repo_privacy.py

    # 扫描任意目录（发布快照，无需是 git 仓库）
    python scripts/check_public_repo_privacy.py --scan-dir dist/public-release/seasight
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Windows 控制台默认 GBK：只重配置输出编码，不改变扫描语义。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]

# 命中即视为“公开前必须处理”的硬标记。
# 标记用拼接方式写出，避免扫描器自身被自己的常量误判。
HARD_MARKERS = (
    "C:\\Users\\",
    "C:" + "/Users/",
    "/" + "Users/",
    "Desktop\\seahawk",
    "Desktop" + "/seahawk",
    "AppData\\",
    "AppData" + "/",
    ".".join(("8", "153", "151", "13")),
)

# 命中即视为“验收/工作日志类敏感产物，公开前需脱敏或排除”。
SENSITIVE_PARTS = (
    "artifacts/harness_dispatch/",
    "artifacts/prod-login-verification/",
    "artifacts/verify-prod-login.cjs",
    "artifacts/verify-map.cjs",
    "artifacts/agent-real-state-acceptance/",
    "artifacts/browser-acceptance/",
    "artifacts/fault-acceptance/",
    "artifacts/map-verification/",
    "artifacts/ui-responsive/",
    "artifacts/metrics/",
    "artifacts/agent_evals/",
    "artifacts/detections-",
    "artifacts/marine-detections-",
    "artifacts/vision-preview/",
)

# 命中只给提示，不阻断：演示密码/哈希属于源码内的测试种子数据，
# 公开前人工确认一遍即可。
NOTICE_PARTS = (
    "deploy/emqx/bootstrap.csv",
    "backend/db/init/02_seed.sql",
)


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git ls-files 失败：{result.stderr.strip()}")
    return [p for p in result.stdout.split("\0") if p]


def files_under(root: Path) -> list[str]:
    """列出 root 下的普通文件（POSIX 相对路径），跳过 .git 目录。"""
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if ".git" in rel_parts:
            continue
        files.append(path.relative_to(root).as_posix())
    return files


def scan_file(rel: str, root: Path) -> tuple[list[str], list[str]]:
    path = root / rel
    hard_hits: list[str] = []
    notice_hits: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return hard_hits, notice_hits

    for marker in HARD_MARKERS:
        if marker in text:
            hard_hits.append(marker)
    for part in NOTICE_PARTS:
        if part in rel:
            notice_hits.append(part)
    return hard_hits, notice_hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="公开仓库发布前的隐私/脱敏扫描")
    parser.add_argument(
        "--scan-dir",
        type=Path,
        default=None,
        help="扫描指定目录而非 git 已跟踪文件（用于公开导出快照）",
    )
    args = parser.parse_args(argv)

    if args.scan_dir is not None:
        root = args.scan_dir.resolve()
        if not root.is_dir():
            print(f"[privacy-scan-error] 目录不存在：{root}")
            return 2
        files = files_under(root)
        scope = f"目录 {root}"
    else:
        root = ROOT
        try:
            files = tracked_files()
        except RuntimeError as exc:
            print(f"[privacy-scan-error] {exc}")
            return 2
        scope = "git 已跟踪文件"

    hard: dict[str, list[str]] = {}
    sensitive: list[str] = []
    notices: list[str] = []

    for rel in files:
        if rel == "scripts/check_public_repo_privacy.py":
            continue
        path_hits, notice_hits = scan_file(rel, root)
        if path_hits:
            hard[rel] = sorted(set(path_hits))
        if any(rel.startswith(part.rstrip("/")) for part in SENSITIVE_PARTS):
            sensitive.append(rel)
        if notice_hits:
            notices.extend(n for n in notice_hits if n not in notices)

    print("公开仓库隐私扫描")
    print(f"- 扫描范围：{scope}")
    print(f"- 扫描文件：{len(files)}")
    print(f"- 本机路径/生产地址命中：{len(hard)} 个文件")
    print(f"- 敏感验收证据路径：{len(sensitive)} 个文件")
    print(f"- 演示种子数据提示：{len(notices)} 项")
    print()

    if hard:
        print("⚠ 以下文件包含本机路径或生产地址，公开前必须脱敏/排除：")
        for rel in sorted(hard):
            print(f"    {rel}  ←  {', '.join(hard[rel])}")
    if sensitive:
        print("⚠ 以下文件属于验收/工作日志类产物，公开前建议排除或单独脱敏：")
        for rel in sorted(sensitive):
            print(f"    {rel}")
    if notices:
        print("· 提示：以下文件含演示账号哈希，公开前确认属于种子数据而非生产凭证：")
        for rel in sorted(notices):
            print(f"    {rel}")

    if hard or sensitive:
        print()
        print("结论：公开前未通过。")
        return 1

    print("✓ 未发现本机路径、生产地址或敏感验收证据")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
