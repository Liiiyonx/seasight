"""扫描工程里「不该提交但没被 .gitignore 覆盖」的文件。

为什么需要它：运行时状态、编译产物、模型权重一旦误提交，
后果往往不是"仓库变脏"这么轻 —— 例如模拟器的 seq 状态文件被提交后，
别人 clone 下来会带着旧的 seq 续跑，导致平台按 (device_id, seq)
去重时静默丢弃事件（无任何报错）。

用法（在 seahawk/ 下）：
    python scripts/check_gitignore.py
"""

from __future__ import annotations

import fnmatch
import os
import sys
from pathlib import Path

# Windows 控制台默认 GBK：结尾的 ✓ / ⚠ 直接 print 会抛 UnicodeEncodeError，
# 把「检查通过」变成退出码 1，进而让 `make check` 在最后一步白白中断。
# 与 check_contract_drift.py / check_claims.py 保持一致，只重配置输出编码，
# 不改变任何扫描语义。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".pytest_cache", ".vite"}

# 明确不该进仓库的（按后缀/文件名/路径片段）
RISKY_SUFFIX = (".pyc", ".pyo", ".log", ".onnx", ".pt", ".engine", ".trt", ".onnxproto")
RISKY_NAME = {".simulator_state.json", ".DS_Store", ".env"}
RISKY_PARTS = ("dist/", "build/", ".egg-info/")


def load_ignore(root: Path) -> list[str]:
    f = root / ".gitignore"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def ignored(rel: str, pats: list[str]) -> bool:
    """近似实现 gitignore 匹配（够用即可，不追求完全等价）。"""
    parts = rel.split("/")
    for pat in pats:
        if pat.startswith("!"):
            continue
        p = pat.lstrip("/")
        if p.endswith("/"):
            d = p.rstrip("/")
            if any(part == d for part in parts[:-1]) or rel.startswith(d + "/"):
                return True
            continue
        if "/" in p:
            if fnmatch.fnmatch(rel, p) or rel.startswith(p.rstrip("*").rstrip("/") + "/"):
                return True
        else:
            if any(fnmatch.fnmatch(part, p) for part in parts):
                return True
    return False


def main() -> int:
    pats = load_ignore(ROOT)
    print(f"读取 .gitignore 规则 {len(pats)} 条")
    print()

    risky: list[str] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            abs_p = Path(dirpath) / name
            rel = abs_p.relative_to(ROOT).as_posix()
            total += 1
            if ignored(rel, pats):
                continue
            if (
                name.endswith(RISKY_SUFFIX)
                or name in RISKY_NAME
                or any(part in rel for part in RISKY_PARTS)
            ):
                risky.append(rel)

    print(f"扫描文件总数（已跳过 node_modules/.git 等）：{total}")
    print()
    if risky:
        print(f"⚠ 有 {len(risky)} 个高风险文件未被 .gitignore 覆盖：")
        for r in sorted(risky):
            print(f"    {r}")
        return 1

    print("✓ 未发现「该忽略但没忽略」的高风险文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
