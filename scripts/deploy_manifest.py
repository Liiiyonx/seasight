#!/usr/bin/env python
"""生成本机源码的部署包（tar.gz + SHA256 清单），供上传到生产服务器。

## 解决什么问题

本仓库**没有 git remote**，也没有 rsync；往服务器同步代码只能靠 scp / 手拷。
手拷最容易犯的错不是"漏拷"，而是**多拷**：把 `.env*`（真实密钥）、`artifacts/`
（运行产物）、`node_modules/`、模型权重一起传上去，轻则覆盖服务器上
`.env.production` 里的真实密钥，重则把 51 MB 的 `.pt` 权重灌进生产盘。

本脚本把"哪些该传"固化成一份清单，打**一个 tar 包**，上传就一行：

    python scripts/deploy_manifest.py                 # 生成 artifacts/deploy/*.tar.gz
    scp artifacts/deploy/<包名> <user>@<host>:/tmp/
    # 服务器上：解包到仓库根，再校验
    tar -xzf /tmp/<包名> -C /path/to/seahawk
    sha256sum -c /tmp/<清单名>

## 刻意不放进包里的东西

- `.env` / `.env.production` / 任何密钥 —— 服务器上的生产密钥**绝不能被本机覆盖**；
- `artifacts/`（除非 `--include-artifacts`）—— 运行产物由服务器自己生成；
- `node_modules/` `.venv*/` `dist/` `__pycache__/` `.git/` —— 服务器端容器内自行构建；
- `*.pt` `*.weights` 等模型权重 —— 体积大且非源码。

排除清单与 `scripts/` 之外另一处用到的 worktree 同步逻辑保持同一份口径，
避免"两处规则不一致"导致两台机器拿到的文件集不同。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 排除的目录：生成物、依赖、虚拟环境、版本控制元数据、本机临时草稿。
EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", ".pytest_cache", "dist",
    ".venv", ".venv-analysis", ".venv-nexent", "artifacts",
    ".ruff_cache", "htmlcov", ".mypy_cache",
    ".wp-demo-4k", ".wp-demo-candidates", ".wp-demo-candidates2",
    ".wp-demo-originals", "wp14c-tmp", "wp14d-basetemp", "wp14d-bt2",
}
# 这些是**必须排除**的文件：密钥与本机一次性草稿，多传一个都是事故。
EXCLUDE_FILES = {"$null", ".wp-index-check.html", ".env", ".env.production", ".env.local"}
EXCLUDE_EXT = {".pt", ".weights", ".pyc"}

# 评测产物是 /api/v1/agents/evals/latest 的在线消费源，但服务器上通常有自己的
# 评测产物 —— 默认**不**打包，需要覆盖时用 --include-artifacts 显式声明。
ARTIFACT_INCLUDE = ("artifacts/agent_evals/latest_v2.json",)


def iter_source_files() -> list[Path]:
    """按排除规则收集要打包的文件（排序固定，保证清单可复现）。"""
    files: list[Path] = []
    for cur, dirs, names in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS)
        rel_root = Path(cur).relative_to(ROOT)
        for name in sorted(names):
            rel = (rel_root / name) if str(rel_root) != "." else Path(name)
            if name in EXCLUDE_FILES or rel.suffix.lower() in EXCLUDE_EXT:
                continue
            files.append(rel)
    return files


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成本机源码部署包")
    parser.add_argument(
        "--include-artifacts",
        action="store_true",
        help="把 artifacts/agent_evals/latest_v2.json 一并打进包里（会覆盖服务器上的同名产物）",
    )
    parser.add_argument(
        "--out-dir",
        default=str(ROOT / "artifacts" / "deploy"),
        help="输出目录（默认 artifacts/deploy）",
    )
    args = parser.parse_args(argv)

    files = iter_source_files()
    if args.include_artifacts:
        for rel in ARTIFACT_INCLUDE:
            if (ROOT / rel).is_file():
                files.append(Path(rel))

    files = sorted(set(files))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tar_path = out_dir / f"seasight-src-{stamp}.tar.gz"

    manifest_lines: list[str] = []
    with tarfile.open(tar_path, "w:gz") as archive:
        for rel in files:
            src = ROOT / rel
            archive.add(src, arcname=str(rel))
            manifest_lines.append(f"{sha256_of(src)}  {rel.as_posix()}")

    manifest_name = f"seasight-src-{stamp}.MANIFEST.sha256"
    manifest_path = out_dir / manifest_name
    # ★ newline=""：不加的话 Windows 会把 \n 翻成 \r\n，服务器上 sha256sum -c
    #   会把每行行尾的 \r 当成文件名的一部分，全部报 "No such file or directory"。
    manifest_path.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8", newline="")

    total_bytes = sum((ROOT / rel).stat().st_size for rel in files)
    print(f"包文件   : {tar_path}")
    print(f"校验清单 : {manifest_name}（{len(files)} 个文件）")
    print(f"源码体积 : {total_bytes / 1048576:.1f} MB")
    print()
    print("上传与落地：")
    print(f"  1) scp {tar_path.name} <user>@<host>:/tmp/")
    print("  2) 服务器上：tar -xzf /tmp/<包名> -C <仓库根>")
    print(f"  3) 服务器上：cd <仓库根> && sha256sum -c /tmp/{manifest_name}")
    print()
    print("★ 解包**不会**覆盖服务器上的 .env.production —— 清单里根本没有它，这是刻意的。")
    print("★ prod 编排没有透传 AGENT_* 开关，且生产库没有演示账号：录屏请走本地演示环境。")
    print("  同步后如需确认线上状态，跑 scripts/deploy_verify.py。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
