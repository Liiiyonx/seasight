#!/usr/bin/env python
"""演示审批链路的静态自检闸门 —— 录屏前 30 秒跑一次。

## 为什么需要它

「审批高光」是整支演示视频的核心镜头，但它依赖三个**分散在不同文件里**的
开关同时到位：

    1. 本地 .env 里 AGENT_REQUIRE_APPROVAL_FOR_WRITE=true
    2. docker-compose（或 demo 叠加层）把这个变量透传进 backend 容器
    3. 数据库里真的存在一个 approver 账号（否则审批队列没人能批）

任何一处漏了，现象都是"任务被智能体直接建掉了"或"审批队列永远是空的"——
而这两种现象在录屏时都只表现为"这段没拍到"，不会报错。本脚本把这三点
一次性静态核对掉，把「录屏到一半才发现」变成「开录前就知道」。

## 静态核对（默认，无需任何服务）

    - 读 .env 与进程环境变量，算出后端进程会拿到什么值；
    - 解析两个 compose 文件，算出让 backend 容器生效的 environment；
    - grep 种子 SQL 与 bootstrap 脚本，看 approver 账号有没有来源；
    - ⑤ **账号清单三方一致性**：`bootstrap_demo_users.py`（权威定义）、
      `02_seed.sql`（初始化）、`LoginDemoPanel.vue`（登录页展示的测试账号）
      三个地方列出的账号必须**完全一致**。

### ⑤ 为什么必须查（2026-09-27 线上验收 P0-5 的真实成因）

线上 `https://<PROD_SERVER>/seasight` 登录页把 `approver` 当成测试账号展示，
但生产库里根本没有这个账号 —— 点它登录只会得到 401「用户名或密码错误」。
而当时的登录验收脚本 `artifacts/verify-prod-login.cjs` 里**只列了三个账号**
（admin / operator / viewer），恰好漏掉 approver，于是这个缺口一路带到了
验收报告之外。登录页展示的账号、能被创建的账号、验收脚本覆盖的账号，
这三份清单一旦各写各的，演示当天必然踩空。

## 可选：真实登录探测（--probe-url）

加 `--probe-url https://<host>/seasight` 后，脚本会**真的**去打登录接口：

    - 逐个账号 POST /api/v1/auth/login，确认 HTTP 200 且业务码为 0；
    - 用返回的令牌 GET /api/v1/auth/me，确认返回的角色与定义一致；
    - 打一次审批门禁探针（POST /agents/approvals/<不存在>/decide）：
      admin/approver 必须**不**被 403 拦下（落到"审批不存在"的业务错误），
      operator/viewer 必须**被**拦下（code 1004 / HTTP 403）。

探针刻意只在**不存在的审批 id**上打，因此不写任何数据、不改任何状态。
自签名证书的站点会跳过 TLS 校验（该站点当前就是这种状态），
脚本会把这句如实打印出来，不假装校验证书通过了。

退出码：
    0 = 演示链路三个环节都就位（含 ⑤；加了 --probe-url 则含真实登录）
    1 = 有环节没就位（逐条打印怎么修）
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
COMPOSE_FILES = (
    ROOT / "docker-compose.yml",
    ROOT / "docker-compose.demo.yml",
)
SEED_SQL = ROOT / "backend" / "db" / "init" / "02_seed.sql"
BOOTSTRAP = ROOT / "backend" / "scripts" / "bootstrap_demo_users.py"
DEMO_PANEL = ROOT / "frontend" / "src" / "components" / "LoginDemoPanel.vue"

FLAG = "AGENT_REQUIRE_APPROVAL_FOR_WRITE"

# 业务错误码：与 backend/app/core/exceptions.py 保持一致
FORBIDDEN_CODE = 1004
# 审批权限白名单：与 backend/app/api/v1/agents.py 的 APPROVER_ROLES 一致
APPROVER_ROLES = ("admin", "approver")


# ----------------------------------------------------------------------
# 账号清单解析：三方（bootstrap / seed / 登录页）必须一致
# ----------------------------------------------------------------------
def parse_bootstrap_accounts(text: str) -> list[tuple[str, str, str]]:
    """从 bootstrap_demo_users.py 解析 `DemoAccount("用户名", "口令", …, UserRole.X)`。

    返回 [(username, password, role_lower), ...]。

    ★ 这是账号的**权威定义**：能被 `make bootstrap-demo-users` 创建的账号
      以它为准，另两处（种子 SQL、登录页）都必须与它对齐。
    """
    pattern = re.compile(
        r"DemoAccount\(\s*"
        r"[\"'](?P<username>[^\"']+)[\"']\s*,\s*"
        r"[\"'](?P<password>[^\"']+)[\"']\s*,\s*"
        r"(?:[\"'][^\"']*[\"']\s*,\s*)?"
        r"UserRole\.(?P<role>[A-Z_]+)",
        re.DOTALL,
    )
    return [
        (m.group("username"), m.group("password"), m.group("role").lower())
        for m in pattern.finditer(text)
    ]


def parse_seed_usernames(text: str) -> list[str]:
    """从 02_seed.sql 的 t_user 插入段里取用户名（只认该段，避免误抓别处字符串）。"""
    match = re.search(
        r"INSERT\s+INTO\s+t_user\s*\(([^)]*)\)\s*VALUES(?P<body>.*?);",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if match is None:
        return []
    return re.findall(r"^\s*\(\s*'([^']+)'", match.group("body"), flags=re.MULTILINE)


def parse_panel_accounts(text: str) -> list[tuple[str, str]]:
    """从 LoginDemoPanel.vue 的 accounts 常量里取登录页展示的账号。"""
    block = re.search(r"const\s+accounts\s*=\s*\[(?P<body>.*?)\n\]", text, flags=re.DOTALL)
    if block is None:
        return []
    pattern = re.compile(
        r"username:\s*'(?P<username>[^']+)'\s*,\s*password:\s*'(?P<password>[^']+)'"
    )
    return [(m.group("username"), m.group("password")) for m in pattern.finditer(block.group("body"))]


# ----------------------------------------------------------------------
# 真实登录探测
# ----------------------------------------------------------------------
def _http_json(
    url: str,
    *,
    payload: dict | None = None,
    token: str | None = None,
    timeout: int = 20,
) -> tuple[int, dict]:
    """打一个 JSON 接口，返回 (HTTP 状态码, 响应体)。

    ★ 自签名证书：目标站点当前用自签名证书（浏览器会告警），
      所以这里显式关掉校验并**在输出里声明**，不假装证书是好的。
      换成正式证书后应删除这段。
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data is not None else "GET"
    )
    try:
        with urllib.request.urlopen(request, context=ctx, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def probe_demo_accounts(
    base_url: str,
    accounts: list[tuple[str, str, str]],
) -> tuple[list[str], list[str]]:
    """逐个账号真实登录 + 角色核对 + 审批门禁探针。

    返回 (problems, printable_lines)。
    """
    base = base_url.rstrip("/")
    problems: list[str] = []
    lines: list[str] = []

    for username, password, role in accounts:
        status, body = _http_json(
            f"{base}/api/v1/auth/login",
            payload={"username": username, "password": password},
        )
        if status != 200 or body.get("code") != 0:
            problems.append(
                f"账号 {username} 登录失败：HTTP {status} · code={body.get('code')} · "
                f"{body.get('message')} —— 该账号在目标库里不存在或口令不一致；"
                "补建命令：生产用 `make prod-bootstrap-demo-users`，"
                "本地开发用 `make bootstrap-demo-users`"
            )
            lines.append(f"  {username:<9} 登录 ❌  HTTP {status}  {body.get('message')}")
            continue

        data = body.get("data") or {}
        token = data.get("access_token")
        actual_role = data.get("role")
        lines.append(f"  {username:<9} 登录 ✅  角色 {actual_role}")
        if actual_role != role:
            problems.append(
                f"账号 {username} 登录返回的角色是 {actual_role!r}，"
                f"与 bootstrap_demo_users.py 定义的 {role!r} 不一致"
            )

        # ---- /auth/me：确认令牌真的能被服务端认出来 ----
        me_status, me_body = _http_json(f"{base}/api/v1/auth/me", token=token)
        me_role = (me_body.get("data") or {}).get("role")
        if me_status != 200 or me_body.get("code") != 0 or me_role != role:
            problems.append(
                f"账号 {username} 的令牌在 /auth/me 上没被认成 {role}："
                f"HTTP {me_status} · code={me_body.get('code')} · role={me_role!r}"
            )
        else:
            lines.append(f"  {'':<9} /me    ✅  令牌有效，can_write="
                         f"{(me_body.get('data') or {}).get('can_write')}")

        # ---- 审批门禁探针（在不存在的审批 id 上打，不写任何数据）----
        gate_status, gate_body = _http_json(
            f"{base}/api/v1/agents/approvals/__probe_not_exists__/decide",
            payload={"decision": "approved"},
            token=token,
        )
        gate_code = gate_body.get("code")
        forbidden = gate_status == 403 or gate_code == FORBIDDEN_CODE
        should_allow = role in APPROVER_ROLES
        if should_allow and forbidden:
            problems.append(
                f"账号 {username}（{role}）本应可以审批，却被门禁拦下："
                f"HTTP {gate_status} · code={gate_code} · {gate_body.get('message')}"
            )
            lines.append("  " + " " * 9 + "审批门禁 ❌ 被拒（应放行）")
        elif not should_allow and not forbidden:
            problems.append(
                f"账号 {username}（{role}）不该有审批权，却通过了门禁："
                f"HTTP {gate_status} · code={gate_code} —— 权限分离失效"
            )
            lines.append("  " + " " * 9 + "审批门禁 ❌ 通过（应拒绝）")
        else:
            verdict = "放行（审批不存在）" if should_allow else "拒绝（无审批权限）"
            lines.append(f"  {'':<9} 审批门禁 ✅ {verdict}")

    return problems, lines


def read_env_file(path: Path) -> dict[str, str]:
    """读取 dotenv 风格的键值对（忽略注释与空行，不处理变量展开）。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def compose_environment(path: Path) -> dict[str, str]:
    """解析 compose 文件里 services.backend.environment（不引入 yaml 依赖）。

    只做缩进级别的最小解析：找到 `backend:` → 找到它下面的 `environment:` →
    收集更深缩进的 `KEY: value`。够用且不引入额外依赖（本项目遵守
    「内核不依赖外部基础设施」的同款纪律：检查脚本也尽量零依赖）。
    """
    if not path.is_file():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines()
    result: dict[str, str] = {}
    in_backend = False
    in_env = False
    backend_indent = env_indent = 0
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if re.fullmatch(r"backend:\s*", stripped):
            in_backend, backend_indent = True, indent
            in_env = False
            continue
        if in_backend and stripped == "environment:":
            in_env, env_indent = True, indent
            continue
        # 退出 backend 段落：同级或更浅的另一个服务键
        if in_backend and not in_env and indent <= backend_indent and stripped.endswith(":"):
            in_backend = False
        if in_env:
            if indent <= env_indent:
                in_env = False
                continue
            if ":" in stripped:
                key, _, value = stripped.partition(":")
                result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def resolve(value: str, env_values: dict[str, str], env_file_values: dict[str, str]) -> str:
    """展开 compose 的 `${VAR:-default}` 写法，模拟 compose 的取值顺序。

    compose 的取值顺序是「进程环境 > compose 目录下的 .env > 字面默认值」，
    这里照此实现，避免得出与实际部署不同的结论。
    """
    match = re.fullmatch(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}", value)
    if not match:
        return value
    name, default = match.group(1), match.group(2) or ""
    return env_values.get(name) or env_file_values.get(name) or default


def set_flag(value: str) -> int:
    """把本地 .env 里的审批开关改成 value（.env 不存在则从模板生成）。

    为什么由本脚本负责改而不是让演示者手改文件：
    手改 .env 的时候很容易把 `=true` 看成 `=True`，或者改错到别的键上，
    结果就是"配置看起来对了、审批却没触发"。这里做的是精确的单键替换，
    改完立刻回读校验，避免这类静默失败。

    ★ 只动本地 .env，**绝不动 .env.example 与 config.py 的默认值**：
      仓库默认必须保持保守（默认不要求审批 = 与生产策略一致），
      演示开关只开在本地。
    """
    if not ENV_FILE.is_file():
        if not ENV_EXAMPLE.is_file():
            print(f"缺少 {ENV_EXAMPLE}，无法生成 .env", file=sys.stderr)
            return 1
        ENV_FILE.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"已从 .env.example 生成 {ENV_FILE.name}")
    text = ENV_FILE.read_text(encoding="utf-8")
    if not re.search(rf"^{FLAG}\s*=", text, flags=re.MULTILINE):
        text = text.rstrip("\n") + f"\n{FLAG}={value}\n"
    else:
        text = re.sub(rf"^{FLAG}\s*=.*$", f"{FLAG}={value}", text, count=1, flags=re.MULTILINE)
    ENV_FILE.write_text(text, encoding="utf-8", newline="")
    # 回读校验：改完必须真的读到目标值，避免"写进去了但没生效"
    actual = read_env_file(ENV_FILE).get(FLAG)
    ok = actual == value
    print(f"{FLAG} → {actual!r} {'✅' if ok else '❌ 写入未生效'}")
    return 0 if ok else 1


def is_local_target(url: str) -> bool:
    """URL 是否指向本机（即「本地开发/演示环境」）？

    ★ 为什么需要它
    ──────────────
    静态自检里 ①（本地 `.env` 的开关）与 ③（compose 透传后的开关注入值）
    描述的是**开发/演示环境**的配置。生产服务器上这两件事**本就不该成立**：

      · 服务器上没有 `.env`，只有 `.env.production`；
      · 生产编排刻意**不**透传 AGENT_* 开关（与「生产默认与生产策略一致」相符），
        审批高光只在本地/演示机录。

    不区分目标的话，在服务器上跑 `make check-demo-accounts PROBE_URL=https://<host>/...`
    会稳定报两条「未就位」并 exit 1 —— 而四账号真实登录其实全 ✅。
    运维看到「未就位」会去修一个**不该被修**的东西，甚至被诱导去生产开审批开关。
    """
    host = urlsplit(url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="演示审批链路静态自检 / 本地开关")
    parser.add_argument(
        "--set",
        choices=("true", "false"),
        default=None,
        help="把本地 .env 的审批开关改成指定值（只动 .env，不动仓库默认值）",
    )
    parser.add_argument(
        "--probe-url",
        default=None,
        metavar="URL",
        help=(
            "对目标站点做真实登录探测，例如 https://<PROD_SERVER>/seasight 。"
            "逐个账号登录 + 校验角色 + 打审批门禁探针（只读，不写数据）"
        ),
    )
    args = parser.parse_args(argv)
    if args.set is not None:
        return set_flag(args.set)

    problems: list[str] = []
    notes: list[str] = []

    # 目标是不是远端（生产/测试服务器）？是的话，①③ 这两条「开发环境配置」
    # 就不适用 —— 详见 is_local_target 的 docstring。
    remote_target = bool(args.probe_url) and not is_local_target(args.probe_url)

    env_file_values = read_env_file(ENV_FILE)
    env_values = {k: v for k, v in os.environ.items()}

    # ---- 1. 本地 .env ----
    if not ENV_FILE.is_file():
        msg = (
            f"① 本地 .env 不存在；{FLAG} 未在开发环境开启"
        )
        (notes if remote_target else problems).append(
            msg + (
                "（目标是远端站点，本条不适用：服务器只有 .env.production，"
                "且生产刻意不开审批开关）"
                if remote_target
                else f" —— 执行 `cp .env.example .env`，并把 {FLAG} 置为 true"
            )
        )
        local_flag = None
    else:
        local_flag = env_file_values.get(FLAG)
        if local_flag != "true":
            problems.append(
                f"① 本地 .env 里 {FLAG}={local_flag!r}，应为 'true'"
                "（演示要拍「人批准后才动手」，开关必须是开的）"
            )

    # ---- 2. 仓库默认必须保持保守 ----
    example_flag = read_env_file(ENV_EXAMPLE).get(FLAG) if ENV_EXAMPLE.is_file() else None
    if example_flag == "true":
        problems.append(
            f"② .env.example 里 {FLAG}=true —— 仓库默认必须保守，"
            "演示开关只开在本地 .env（否则克隆仓库的人默认就带审批，"
            "会与「默认与生产策略一致」的说法矛盾）"
        )

    # ---- 3. compose 透传 ----
    container_value = None
    for compose in COMPOSE_FILES:
        env_block = compose_environment(compose)
        if FLAG in env_block:
            container_value = resolve(env_block[FLAG], env_values, env_file_values)
    if container_value is None:
        problems.append(
            f"③ 两个 compose 文件都没有把 {FLAG} 透传给 backend —— "
            "容器内不会读到它（容器不读仓库根 .env，只有 compose 变量插值会）"
        )
    elif container_value != "true":
        msg = (
            f"③ 按当前配置，backend 容器实际会拿到 {FLAG}={container_value!r}，不是 'true'"
        )
        (notes if remote_target else problems).append(
            msg + (
                "（目标是远端站点，本条不适用：生产编排刻意不透传 AGENT_*，"
                "审批高光只在本地/演示机录）"
                if remote_target
                else ""
            )
        )

    # ---- 4. approver 账号来源 ----
    seed_text = SEED_SQL.read_text(encoding="utf-8") if SEED_SQL.is_file() else ""
    bootstrap_text = BOOTSTRAP.read_text(encoding="utf-8") if BOOTSTRAP.is_file() else ""
    seed_has_approver = bool(re.search(r"'approver'\s*,", seed_text))
    # 双引号也要认：bootstrap_demo_users.py 用的是 DemoAccount("approver", ...)
    # ——只按单引号找会得出「没有 approver」的错误结论（本脚本第一版就栽在这）。
    bootstrap_has_approver = bool(re.search(r"['\"]approver['\"]", bootstrap_text))
    bootstrap_has_role = "UserRole.APPROVER" in bootstrap_text
    if not seed_has_approver:
        problems.append(
            "④ 02_seed.sql 里没有 approver 账号 —— `make db-init` 之后审批队列"
            "无第二人可批（注意 02_seed.sql 会 TRUNCATE t_user）"
        )
    if not bootstrap_has_approver:
        problems.append(
            "④ bootstrap_demo_users.py 里没有 approver 账号 —— "
            "`make bootstrap-demo-users` 也补不出来"
        )
    elif not bootstrap_has_role:
        problems.append(
            "④ bootstrap_demo_users.py 里有 approver 账号，但角色不是 "
            "UserRole.APPROVER —— 它批不了（审批权守卫只认 admin/approver）"
        )

    # ---- 5. 账号清单三方一致性 ----
    #      bootstrap（权威定义） == 02_seed.sql == 登录页展示
    #      ★ 线上 P0-5 的成因就是这三份清单各写各的：登录页展示了 approver，
    #        生产库里没有；而登录验收脚本又只覆盖了三个账号，缺口没被兜住。
    bootstrap_accounts = parse_bootstrap_accounts(bootstrap_text)
    seed_names = parse_seed_usernames(seed_text)
    panel_accounts = parse_panel_accounts(
        DEMO_PANEL.read_text(encoding="utf-8") if DEMO_PANEL.is_file() else ""
    )
    bootstrap_names = [item[0] for item in bootstrap_accounts]
    panel_names = [item[0] for item in panel_accounts]

    # 自证：三处都解析出东西，否则下面的"一致"是假绿
    if not bootstrap_accounts:
        problems.append(
            "⑤ 没能从 bootstrap_demo_users.py 解析出任何账号 —— "
            "解析器失效了（DemoAccount(...) 的写法变了？），本次一致性核对无效"
        )
    if not seed_names:
        problems.append("⑤ 没能从 02_seed.sql 的 t_user 段解析出账号 —— 解析器失效")
    if not panel_accounts:
        problems.append(
            "⑤ 没能从 LoginDemoPanel.vue 解析出展示账号 —— 解析器失效"
        )

    for label, names in (
        ("02_seed.sql", seed_names),
        ("LoginDemoPanel.vue", panel_names),
    ):
        missing = [n for n in bootstrap_names if n not in names]
        extra = [n for n in names if n not in bootstrap_names]
        if missing:
            problems.append(
                f"⑤ {label} 缺少账号 {missing}（bootstrap 里定义了但这里没有）"
                " —— 初始化/展示与权威定义不一致"
            )
        if extra:
            problems.append(
                f"⑤ {label} 多出账号 {extra}（bootstrap 里没有定义）"
                " —— 这份清单里的账号可能根本登不上"
            )

    # 登录页的口令必须与权威定义一致（演示面板会一键填入，错了就登不上）
    bootstrap_pw = {name: pw for name, pw, _ in bootstrap_accounts}
    for username, password in panel_accounts:
        if username in bootstrap_pw and bootstrap_pw[username] != password:
            problems.append(
                f"⑤ 登录页展示的 {username} 口令与 bootstrap 定义不一致 —— "
                "演示面板一键填入的凭据会登录失败"
            )

    # ---- 输出 ----
    print("=" * 66)
    print("  演示审批链路 · 静态自检")
    if remote_target:
        print(f"  （目标 {args.probe_url} 是远端站点：①②③ 属开发/演示环境项，仅作参考）")
    print("=" * 66)
    print(f"  .env                : {'存在' if ENV_FILE.is_file() else '缺失'}"
          f" · {FLAG}={local_flag!r}")
    print(f"  .env.example（仓库默认）: {FLAG}={example_flag!r}"
          f" {'✅ 保守' if example_flag != 'true' else '❌ 被改成 true'}")
    print(f"  backend 容器将拿到   : {FLAG}={container_value!r}"
          f"{'（开发 compose 口径）' if remote_target else ''}")
    print(f"  approver · 种子 SQL  : {'✅ 有' if seed_has_approver else '❌ 无'}")
    print(f"  approver · bootstrap : "
          f"{'✅ 有' if bootstrap_has_approver and bootstrap_has_role else '❌ 无/角色不对'}")
    print(f"  账号清单三方一致     : "
          f"{'✅ ' + '/'.join(bootstrap_names) if bootstrap_names else '❌ 解析失败'}")
    print("-" * 66)

    # ---- 6. 可选：真实登录探测 ----
    #      ★ 前面全是静态核对，证明的是「配置链路是通的」；
    #        只有这一步能证明「服务真的认得这些账号」——
    #        线上 P0-5（approver 在生产库里不存在）正是静态核对查不出来的。
    if args.probe_url:
        print()
        print("=" * 66)
        print("  真实登录探测")
        print("=" * 66)
        print(f"  目标：{args.probe_url}")
        print("  ⚠️  自签名证书站点：本次跳过 TLS 证书校验（不表示证书已验证通过）")
        print("-" * 66)
        probe_problems, probe_lines = probe_demo_accounts(
            args.probe_url, bootstrap_accounts
        )
        for line in probe_lines:
            print(line)
        print("-" * 66)
        problems.extend(probe_problems)

    if notes:
        print()
        print("仅参考（开发/演示环境项，不影响本次结论）：")
        for item in notes:
            print(f"  · {item}")

    if problems:
        print("未就位：")
        for item in problems:
            print(f"  · {item}")
        print("-" * 66)
        print("修完之后再跑一次本脚本；运行时确认用 `make demo-approval-check`。")
        return 1

    print("演示链路各环节都就位。接下来：")
    if args.probe_url:
        print("  make prod-bootstrap-demo-users   # 生产：确保四个账号都在（尤其 approver）")
    else:
        print("  make demo-approval-up        # 起服务（带审批策略）")
        print("  make bootstrap-demo-users    # 本地开发：确保四个账号都在（尤其 approver）")
    print("  make demo-approval-check     # 向内核确认真会拦 WRITE 动作")
    if not args.probe_url:
        print()
        print("提示：加 `--probe-url https://<host>/seasight` 可真实登录一遍，")
        print("      确认目标库里四个账号都能登上（静态核对查不出来这一层）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
