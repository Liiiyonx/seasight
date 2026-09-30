"""契约校验：前端调用的 API 路径 vs 后端实际注册的路由。

用途：前端与后端分开开发时，最容易出的问题是「前端调了个不存在的接口」，
而这类错误只在运行时才暴露（点击某个页签 → 404）。
这个脚本把两边对齐检查提前到提交前。

用法（在 seahawk/ 仓库根目录下，任意 python 都能跑）：
    python scripts/check_api_contract.py

退出码：0 = 全部对齐；1 = 存在前端调用了但后端没实现的路径。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Windows 控制台默认 GBK：中文路径清单会以乱码输出（详见 check_contract_drift.py）。
# 只重配置 stdout/stderr 编码，不改变任何契约校验语义。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# 本文件在 seahawk/scripts/ 下，所以：
#   ROOT     = seahawk/
#   BACKEND  = seahawk/backend/   ← 需要加进 sys.path 才能 import app.*
ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT / "backend"

API_JS = ROOT / "frontend" / "src" / "api" / "index.js"


def backend_paths() -> set[tuple[str, str]]:
    sys.path.insert(0, str(BACKEND_DIR))
    from app.main import create_app  # noqa: PLC0415

    spec = create_app().openapi()
    out: set[tuple[str, str]] = set()
    for path, ops in spec["paths"].items():
        for method in ops:
            if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                out.add((method.upper(), path))
    return out


def normalize(path: str) -> str:
    """把路径参数统一成 {param}，便于比较。/api/v1 前缀补全。"""
    path = re.sub(r"\$\{[^}]+\}", "{param}", path)      # JS 模板变量
    path = re.sub(r"\{[^}]+\}", "{param}", path)        # 已是占位符
    if not path.startswith("/api/v1") and path.startswith("/"):
        path = "/api/v1" + path
    return path


def frontend_calls() -> set[str]:
    src = API_JS.read_text(encoding="utf-8")
    # 抓 request.get('/events') 这类调用里的第一个字符串参数
    raw = re.findall(r"""\.\s*(?:get|post|put|patch|delete)\s*\(\s*[`'"]([^`'"]+)[`'"]""", src)
    return {normalize(p) for p in raw}


def main() -> int:
    impl = backend_paths()
    impl_paths = {normalize(p) for _, p in impl}
    fe = frontend_calls()

    print(f"后端注册端点 : {len(impl)} 条（{len(impl_paths)} 个唯一路径）")
    print(f"前端调用路径 : {len(fe)} 个")
    print()

    missing = sorted(p for p in fe if p not in impl_paths)

    print("-- 前端调用 -> 后端实现 --")
    for p in sorted(fe):
        methods = sorted(m for m, pp in impl if normalize(pp) == p)
        if methods:
            print(f"  OK      {p:<40} [{','.join(methods)}]")
        else:
            print(f"  缺失    {p:<40} <- 后端没有这个路径")

    print()
    if missing:
        print(f"[FAIL] 有 {len(missing)} 个前端调用找不到后端实现：")
        for p in missing:
            print(f"    {p}")
        print()
        print("  可能原因：① 路径写错；② 后端路由没注册；③ 改了后端忘了同步前端。")
        return 1

    print("[OK] 前端调用的每个路径在后端都有实现")

    # 反向：后端有但前端没调（只是提示，不算失败）
    unused = sorted(impl_paths - fe)
    if unused:
        print()
        print(f"[INFO] 后端有但前端未调用的路径（{len(unused)} 个，仅提示）：")
        for p in unused:
            print(f"    {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
