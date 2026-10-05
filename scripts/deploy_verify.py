#!/usr/bin/env python
"""核对生产服务器上的探海灵眸是不是最新构建（只读，不改服务器任何东西）。

## 解决什么问题

「同步到服务器」最容易出的错不是传不上去，而是**传上去了但没生效**：
前端是容器内构建的，`make prod-up` 没跑、或 `VITE_BASE_PATH` 变了没重建，
都会出现"代码同步了、页面还是旧的"。而且这台服务器**还跑着聆心**，
靠肉眼判断"现在线上到底是哪个版本"既慢又容易自我说服。

本脚本只读做四件事：

    1. 拉线上 index.html，取出它实际引用的 index-*.js 文件名；
    2. 与本地最近一次构建（frontend/dist 或指定目录）比对，判定 同步/落后；
    3. 在线上 JS 里 grep 本轮新能力的特征串（回放倍速、口径字幕、
       require_approval_for_write、经验库、研判），报告哪些在线上**没有**；
    4. 探 /seasight/health，确认后端还活着。

## 为什么用特征串而不是版本号

项目没有发布版本号，git HEAD 在服务器上也可能对不上（仓库没有 remote）。
特征串是从**本轮实际改动**里挑的、且都出现在打包产物里的字符串；
每加一轮能力就往 `MARKERS` 里补一条 —— 这份清单本身就是"这版新增了什么"的台账。

用法：
    python scripts/deploy_verify.py                       # 默认读 SEASIGHT_PRODUCTION_URL
    python scripts/deploy_verify.py --dist frontend/dist  # 指定本地构建目录
"""

from __future__ import annotations

import argparse
import os
import re
import ssl
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = os.environ.get(
    "SEASIGHT_PRODUCTION_URL", "https://127.0.0.1/seasight/"
)

# 本轮能力的特征串：线上对应 chunk 里命中 = 该能力已上线。
#
# ★ 必须按「它住在哪个 chunk」分组，否则会全部误报"未上线"：
#   智能体控制台是**路由懒加载**，代码在独立的 `AgentsView-*.js` 分包里，
#   入口 `index-*.js` 里只有一句 `import("./AgentsView-xxx.js")` ——
#   只 grep 入口包，等于在错误的文件里找东西（本脚本第一版就栽在这）。
#   第一版就因为这个把"已上线"判成了"未上线"。
# ★ 每次新增面向演示的能力，往这里补一条；它们必须真的出现在打包产物里。
MARKERS = {
    "轨迹回放倍速/暂停（replay-speeds）": ("agentsview", "replay-speeds"),
    "审批策略自检字段（require_approval_for_write）": ("agentsview", "require_approval_for_write"),
    "跨 run 经验库": ("agentsview", "经验库"),
    "多角色研判": ("agentsview", "研判"),
    "口径字幕（合成事件）": ("entry", "合成事件"),
}

# 入口 chunk 里的懒加载引用形如 `import("./AgentsView-AbCd1234.js")`，
# 从这里就能拿到分包文件名，不用去猜 hash。
AGENTS_VIEW_CHUNK_RE = re.compile(r"AgentsView-([A-Za-z0-9_-]+)\.js")

# 本地构建目录候选（按顺序找第一个存在的）
LOCAL_DIST_CANDIDATES = ("frontend/dist", "frontend/build")


def fetch(url: str, *, insecure: bool, timeout: int) -> str:
    context = None
    if insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, headers={"User-Agent": "seasight-deploy-verify/1.0"})
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return response.read().decode("utf-8", errors="replace")


def local_bundle_dir(explicit: str | None) -> Path | None:
    candidates = [explicit] if explicit else list(LOCAL_DIST_CANDIDATES)
    for candidate in candidates:
        if candidate and (ROOT / candidate).is_dir():
            return ROOT / candidate
    return None


def local_dist_is_stale(dist_dir: Path) -> bool:
    """本地 dist 是否落后于源码（落后就不能当"本地最新"来比）。

    为什么要有这个判定：本机 `npm run build` 曾被沙箱的删除保护拦住过，
    导致 dist 长期停在旧版本。拿一个过期 dist 去"比对线上"，会得出
    「线上落后」的错误结论 —— 其实两边都旧。
    """
    newest_src = 0.0
    for pattern in ("src/**/*.vue", "src/**/*.js", "src/**/*.css", "index.html", "vite.config.js"):
        for path in (ROOT / "frontend").glob(pattern):
            newest_src = max(newest_src, path.stat().st_mtime)
    newest_dist = 0.0
    for path in dist_dir.rglob("*"):
        if path.is_file():
            newest_dist = max(newest_dist, path.stat().st_mtime)
    return newest_src > newest_dist


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="核对线上 Oceanus 构建版本（只读）")
    parser.add_argument("--url", default=DEFAULT_URL, help="线上入口（默认生产路径前缀）")
    parser.add_argument("--dist", default=None, help="本地构建目录（默认自动找 frontend/dist）")
    parser.add_argument(
        "--no-insecure",
        action="store_true",
        help="证书是共享域名的自签证书时需要跳过校验；加此参数则严格校验",
    )
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args(argv)

    base = args.url if args.url.endswith("/") else args.url + "/"
    insecure = not args.no_insecure
    problems: list[str] = []

    print("=" * 68)
    print(f"  线上版本核对 · {base}")
    print("=" * 68)

    # ---- 1. 线上 index.html ----
    try:
        index_html = fetch(base, insecure=insecure, timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001 —— 探测失败要给出可操作的结论
        print(f"❌ 拉不到线上页面：{exc}")
        print("   （服务器不可达 / 路径不对 / 本机无外网）")
        return 1

    bundle_match = re.search(r'src="([^"]*assets/index-[^"]+\.js)"', index_html)
    if not bundle_match:
        print("❌ index.html 里找不到 index-*.js —— 页面结构可能变了，人工看一眼")
        return 1
    live_bundle_path = bundle_match.group(1)
    live_bundle_name = live_bundle_path.rsplit("/", 1)[-1]
    print(f"  线上 bundle : {live_bundle_name}")

    # ---- 2. 与本地构建比对 ----
    dist_dir = local_bundle_dir(args.dist)
    local_name = None
    if dist_dir is not None:
        local_bundles = sorted(
            p.name for p in (dist_dir / "assets").glob("index-*.js")
        ) if (dist_dir / "assets").is_dir() else []
        if local_bundles:
            local_name = local_bundles[-1]
            stale = local_dist_is_stale(dist_dir)
            print(f"  本地 bundle : {local_name}  （{dist_dir}）")
            if stale:
                # ⚠️ 这台机器上 mtime 不可靠（有文件被"碰"过但内容没变），
                #    所以这里只做提醒不做结论，避免把人带偏。
                print("     ⚠️ 本地 dist 的 mtime 早于源码 —— 若你确认没改过源码可忽略；"
                      "拿不准就重新 `npm run build` 再核对")
    if local_name is None:
        print("  本地 bundle : 未找到本地构建 —— 先 `npx vite build`（或 make prod-build）")
    elif local_name == live_bundle_name:
        print("  ✅ 线上与本地构建一致（前端已同步）")
    else:
        # ★ 名字不一致 ≠ 落后：生产构建注入了 VITE_BASE_PATH=/seasight/，
        #   本地默认是 /，base 不同 → 产物内容不同 → hash 必然不同。
        #   真正判定"是否生效"的是下面的特征串，bundle 名只作参考。
        print(f"  ⚠️ bundle 名不同（线上 {live_bundle_name} / 本地 {local_name}）——"
              "大概率是 VITE_BASE_PATH 不同，以特征串判定为准")

    # ---- 3. 新能力特征串（按 chunk 分别核对） ----
    # ★ URL 拼接的坑：index.html 里的 src 是**带 base 前缀的绝对路径**
    #   （/seasight/assets/index-xxx.js），不能再拼一次 base —— 拼了就会请求
    #   /seasight/seasight/assets/...，拿到的是 SPA 兜底页（HTTP 200），
    #   于是所有特征串都"未上线"，得出与事实相反的结论（本脚本前两版都栽在这）。
    #   一律用 urljoin 处理。
    from urllib.parse import urljoin

    entry_js = ""
    agentsview_js = ""
    live_js_url = urljoin(base, live_bundle_path)
    try:
        entry_js = fetch(live_js_url, insecure=insecure, timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"拉不到线上入口 JS（{exc}）")

    chunk_match = AGENTS_VIEW_CHUNK_RE.search(entry_js) if entry_js else None
    if chunk_match:
        agents_url = urljoin(live_js_url, f"./AgentsView-{chunk_match.group(1)}.js")
        try:
            agentsview_js = fetch(agents_url, insecure=insecure, timeout=args.timeout)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"拉不到线上 AgentsView 分包（{exc}）")
    else:
        problems.append("入口 JS 里找不到 AgentsView 分包引用（路由可能被改动）")

    if entry_js or agentsview_js:
        print("-" * 68)
        for label, (where, marker) in MARKERS.items():
            body = agentsview_js if where == "agentsview" else entry_js
            ok = bool(body) and marker in body
            print(f"  {'✅ 已上线' if ok else '❌ 未上线'}  {label}")
            if not ok:
                problems.append(f"线上缺少本轮能力：{label}")

    # ---- 4. 后端存活 ----
    health_url = base + "health"
    try:
        health = fetch(health_url, insecure=insecure, timeout=args.timeout)
        state = "ok" if '"status"' in health or health.strip() else "空响应"
        print("-" * 68)
        print(f"  后端 /health : {state}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"后端 /health 不可达：{exc}")

    print("=" * 68)
    if problems:
        print("结论：**未同步/未生效** ——")
        for item in problems:
            print(f"  · {item}")
        print()
        print("处理路径（服务器上执行，本机没有凭据做不了）：")
        print("  1) 本机：python scripts/deploy_manifest.py   # 生成源码包 + SHA256 清单")
        print("  2) scp 包到服务器 /tmp/，解包到仓库根，sha256sum -c 校验")
        print("  3) 服务器上：make prod-up   # 前端在容器内构建，必须重建而不是重启")
        print("     （改过 VITE_BASE_PATH 的话必须 prod-build / prod-up，只重启无效）")
        print("  4) 再跑一次本脚本，直到显示『线上与本地构建一致』")
        return 1

    print("结论：线上与本地一致，本轮能力已全部上线。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
