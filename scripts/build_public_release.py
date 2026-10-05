#!/usr/bin/env python
"""构建 Oceanus 开源发布快照（脱敏副本）。

只导出显式允许清单内的 git 已跟踪文件，避免把私有验收产物、比赛材料和
本机路径带进公开仓库。构建流程：

1. 按允许清单复制文件到 ``dist/public-release/seasight``；
2. 对少量仍含本机路径的文件做确定性改写；
3. 再做一次额外的个人信息/凭证扫描；
4. 生成 SHA256 清单和体积报告；
5. 调用 ``check_public_repo_privacy.py --scan-dir`` 复核快照。

用法（在仓库根目录）：
    python scripts/build_public_release.py
    python scripts/build_public_release.py --dest dist/public-release/seasight
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEST = ROOT / "dist" / "public-release" / "seasight"

INCLUDE_DIRS = (
    "backend",
    "frontend",
    "edge",
    "ml",
    "integrations",
    "deploy",
    "scripts",
    "docs",
)

INCLUDE_ROOT_FILES = (
    ".env.example",
    ".env.production.example",
    ".gitignore",
    "LICENSE",
    "Makefile",
    "README.md",
    "docker-compose.prod.yml",
    "docker-compose.yml",
    "pytest.ini",
)

# 比赛材料、内部证据流程和含个人信息的工具不进公开快照。
EXCLUDE_FILES = {
    "docs/business-model.md",
    "docs/evidence-claim-policy.md",
    "docs/product-readiness.md",
    "scripts/build_huawei_track3_agent_docx.py",
    "scripts/build_huawei_track3_docx.py",
    "scripts/build_plan_docx.py",
    "scripts/check_claims.py",
    "scripts/evidence_registry.py",
    "scripts/generate_lianjiang_drafts.py",
    "scripts/package_huawei_track3_submission.py",
    "scripts/selftest_evidence_gate.py",
    "scripts/selftest_evidence_scripts.py",
    "scripts/selftest_result.txt",
}

EXCLUDE_PREFIXES = (
    "docs/competitions/",
    "docs/evidence-inbox/",
)

# 键为快照内相对路径；每项必须精确命中，避免上游文本变化后静默漏改。
TEXT_REPLACEMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    "README.md": (
        (
            "接口与部署边界见 `docs/competitions/huawei-nexent.md`，MCP 运行说明见",
            "接口与部署边界见 `docs/deployment.md` 与 `docs/architecture.md`，MCP 运行说明见",
        ),
        (
            "报告见 `artifacts/browser-acceptance/latest.json`。",
            "报告由本机验收流程生成，不随仓库发布。",
        ),
        (
            "报告见 `artifacts/fault-acceptance/latest.json`。",
            "报告由本机验收流程生成，不随仓库发布。",
        ),
        (
            "报告见 `artifacts/agent-real-state-acceptance/latest.json`。",
            "报告由本机验收流程生成，不随仓库发布。",
        ),
        (
            "`artifacts/metrics/opencv_latest.json` 为 `not_evaluated`",
            "本机感知指标产物为 `not_evaluated`",
        ),
    ),
    "Makefile": (
        (
            "\t$(PYTHON) scripts/selftest_evidence_scripts.py",
            "\t@echo \"[check-evidence-scripts-selftest] 内部证据脚本不随开源快照发布\"",
        ),
        (
            "\t.venv-analysis/Scripts/python.exe scripts/fault_acceptance.py",
            "\t$(PYTHON) scripts/fault_acceptance.py",
        ),
    ),
    "docs/agent-program-wave3.md": (
        (
            "- 工作目录固定为 `C:\\Users\\Liii\\Desktop\\seahawk`。",
            "- 工作目录固定为仓库根目录；下文相对路径均以仓库根为基准。",
        ),
    ),
    "scripts/knowledge_qa_trace_capture.mjs": (
        (
            "const PLAYWRIGHT_CORE_PATH =\n"
            "  process.env.PLAYWRIGHT_CORE_PATH ||\n"
            "  'C:\\\\Users\\\\Liii\\\\AppData\\\\Roaming\\\\npm\\\\node_modules\\\\@playwright\\\\mcp\\\\node_modules\\\\playwright-core'",
            "const PLAYWRIGHT_CORE_PATH = process.env.PLAYWRIGHT_CORE_PATH || 'playwright-core'",
        ),
        (
            "const BROWSER_PATH =\n"
            "  process.env.SEASIGHT_BROWSER_PATH ||\n"
            "  'C:\\\\Program Files\\\\Google\\\\Chrome\\\\Application\\\\chrome.exe'",
            "const BROWSER_PATH = process.env.SEASIGHT_BROWSER_PATH || undefined",
        ),
    ),
}

# ★ 扫描器自身豁免名单（2026-10-05 补）
# ─────────────────────────────────────────────────────────────
# 这两个文件**定义**了用于检出本机路径/生产地址的正则，
# 所以它们的源码里必然出现这些模式的字面量。扫描自己会永远命中，
# 导致门禁无法通过。
# 豁免是安全的：它们的"命中"是模式定义，不是真的泄露路径。
SCANNER_SELF = frozenset({
    "scripts/build_public_release.py",
    "scripts/precommit_scan.py",
    "scripts/check_public_repo_privacy.py",
})

EXTRA_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("本机用户目录", re.compile(r"C:\\Users\\")),
    ("华为云生产地址", re.compile(r"\b8\.153\.151\.13\b")),
    (
        "中国大陆身份证号",
        re.compile(r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b"),
    ),
    ("Nexent 验收账号", re.compile(r"(?:suadmin|seasight\.acceptance)@nexent\.com")),
    ("私钥块", re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY")),
    ("疑似 API Key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
)

BINARY_SUFFIXES = {
    ".7z",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".otf",
    ".pdf",
    ".png",
    ".ttf",
    ".webp",
    ".woff",
    ".woff2",
    ".xlsx",
    ".zip",
}


def git_tracked_files() -> list[str]:
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


def selected_files() -> list[str]:
    tracked = set(git_tracked_files())
    selected: list[str] = []
    for rel in sorted(tracked):
        in_dir = any(rel == d or rel.startswith(f"{d}/") for d in INCLUDE_DIRS)
        if not in_dir and rel not in INCLUDE_ROOT_FILES:
            continue
        if rel in EXCLUDE_FILES:
            continue
        if any(rel.startswith(prefix) for prefix in EXCLUDE_PREFIXES):
            continue
        selected.append(rel)
    return selected


def apply_replacements(rel: str, text: str) -> str:
    for old, new in TEXT_REPLACEMENTS.get(rel, ()):  # noqa: B007 - 报错信息需要 old
        if old not in text:
            raise RuntimeError(f"脱敏替换未命中：{rel} <- {old[:80]!r}")
        text = text.replace(old, new)
    return text


def copy_payload(dest: Path, files: list[str]) -> None:
    for rel in files:
        src = ROOT / rel
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel in TEXT_REPLACEMENTS:
            text = src.read_text(encoding="utf-8")
            target.write_text(apply_replacements(rel, text), encoding="utf-8", newline="\n")
        else:
            shutil.copy2(src, target)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_files(dest: Path) -> list[str]:
    return sorted(
        path.relative_to(dest).as_posix()
        for path in dest.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(dest).parts
    )


def scan_extra(dest: Path, files: list[str]) -> list[str]:
    hits: list[str] = []
    for rel in files:
        path = dest / rel
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        if path.stat().st_size > 5 * 1024 * 1024:
            continue
        # ★ 检测器自身（以及 precommit 扫描器）必然含有这些模式的字面量 ——
        #   它们定义正则来查别人。豁免，否则永远过不了自己的门禁。
        if rel in SCANNER_SELF:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in EXTRA_PATTERNS:
            if pattern.search(text):
                hits.append(f"{rel}  <-  {label}")
    return hits


def build_report(dest: Path, files: list[str]) -> str:
    total_bytes = sum((dest / rel).stat().st_size for rel in files)
    by_top: dict[str, list[int]] = {}
    for rel in files:
        top = rel.split("/", 1)[0] if "/" in rel else "(根目录)"
        count, size = by_top.get(top, [0, 0])
        by_top[top] = [count + 1, size + (dest / rel).stat().st_size]

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Oceanus 公开快照报告",
        "",
        f"- 生成时间：{generated}",
        f"- 文件数：{len(files)}",
        f"- 总体积：{total_bytes / 1024 / 1024:.2f} MiB",
        "- 来源：私有开发仓库的显式允许清单（非历史推送）",
        "",
        "## 顶层分布",
        "",
        "| 顶层 | 文件数 | 体积 |",
        "| --- | ---: | ---: |",
    ]
    for top in sorted(by_top):
        count, size = by_top[top]
        lines.append(f"| `{top}` | {count} | {size / 1024 / 1024:.2f} MiB |")
    lines.append("")
    return "\n".join(lines)


def run_privacy_scan(dest: Path) -> int:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_public_repo_privacy.py"), "--scan-dir", str(dest)],
        cwd=ROOT,
        check=False,
    )
    return result.returncode


def prepare_dest(dest: Path) -> Path:
    dest = dest.resolve()
    allowed_root = (ROOT / "dist" / "public-release").resolve()
    if dest == allowed_root or allowed_root not in dest.parents:
        if dest.exists():
            raise SystemExit(f"拒绝清理非默认发布目录：{dest}")
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 Oceanus 开源发布快照")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="输出目录")
    args = parser.parse_args(argv)

    try:
        files = selected_files()
    except RuntimeError as exc:
        print(f"[build-public-release] {exc}")
        return 2
    if not files:
        print("[build-public-release] 允许清单为空，已中止")
        return 2

    dest = prepare_dest(args.dest)
    copy_payload(dest, files)

    extra_hits = scan_extra(dest, files)
    if extra_hits:
        print("额外敏感内容扫描未通过：")
        for hit in extra_hits:
            print(f"  {hit}")
        return 1

    report = build_report(dest, files)
    (dest / "PUBLIC_RELEASE_REPORT.md").write_text(report, encoding="utf-8", newline="\n")

    manifest_files = iter_files(dest)
    manifest = "".join(f"{sha256_file(dest / rel)}  {rel}\n" for rel in manifest_files if rel != "MANIFEST.sha256")
    (dest / "MANIFEST.sha256").write_text(manifest, encoding="utf-8", newline="\n")

    print("[build-public-release] 快照已生成")
    print(f"  - 目录：{dest}")
    print(f"  - 文件数（不含 manifest）：{len(manifest_files) - 1}")
    privacy_code = run_privacy_scan(dest)
    if privacy_code != 0:
        print("[build-public-release] 隐私扫描未通过，快照不可发布")
        return privacy_code
    print("[build-public-release] 全部检查通过，可进入 git init / push 步骤")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
