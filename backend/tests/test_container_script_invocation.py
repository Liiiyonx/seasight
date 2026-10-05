"""静态质量守卫：Makefile 里「在容器内跑仓库脚本」的调用写法。

★ 为什么需要这条
────────────────
实测缺陷（2026-09-27，生产服务器 /opt/seasight）：

```make
bootstrap-demo-users:
	docker compose exec -T backend python scripts/bootstrap_demo_users.py
```

这条目标是交付文档里白纸黑字写给运维的「最后一米」命令，实际在服务器上
**连续踩两个雷，两次都不可能成功**：

1. `docker compose exec` 走的是**开发** compose（`docker-compose.yml` +
   仓库根的 `.env`）。生产只用 `.env.production` + `docker-compose.prod.yml`，
   于是插值阶段就死：

   ```
   error while interpolating services.postgres.environment.POSTGRES_PASSWORD:
     required variable POSTGRES_PASSWORD is missing a value
   make: *** [Makefile:165: bootstrap-demo-users] Error 1
   ```

2. 就算把 compose 换对，`python scripts/x.py` 也必失败 —— 镜像 `WORKDIR=/app`
   但没设 `PYTHONPATH`，Python 会把 `sys.path[0]` 设成**脚本所在目录**
   `/app/scripts`，于是脚本首行的 `from app.core.config import settings`：

   ```
   ModuleNotFoundError: No module named 'app'
   ```

   正确写法是 `python -m scripts.bootstrap_demo_users`（`sys.path[0]` = CWD = /app）。
   同理 `prod-create-admin` 也中招。

3. 还有第三个雷：`bootstrap_demo_users.py` 自带生产闸门 ——
   `APP_ENV=production` 且没传 `--confirm-production` 时直接 `SystemExit`。
   所以生产入口必须**同时**满足：prod compose + `-m` + `--confirm-production`。

这类缺陷的恶劣之处：**所有静态检查全绿**。Makefile 语法对、目标存在、
`make help` 里也列得出来、CI 也不跑它。只有真的在服务器上敲一次才会发现，
而「敲一次」恰好是运维最可能省略的一步。

本守卫把这三条形状钉死在 CI 里。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MAKEFILE = "Makefile"
BACKEND_DOCKERFILE = "backend/Dockerfile"

# 在容器里执行仓库脚本的两种写法
BAD_SCRIPT_INVOCATION = re.compile(r"\bexec\b[^\n]*?\bpython\s+scripts/[\w./-]+\.py")
GOOD_SCRIPT_INVOCATION = re.compile(r"\bpython\s+-m\s+scripts\.[\w.]+")

PROD_COMPOSE_MARKER = "$(PROD_COMPOSE)"


def _read(project_root: Path, rel: str) -> str:
    path = project_root / rel
    assert path.is_file(), f"{rel} 不存在 —— 守卫的前提文件被挪走了"
    return path.read_text(encoding="utf-8")


def _recipes(makefile_text: str) -> dict[str, str]:
    """把 Makefile 解析成 {目标名: 整个 recipe 文本（含续行）}。

    只取顶层目标（行首无缩进的 `name:`），跳过变量赋值与 `.PHONY`。
    """
    recipes: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []

    for raw in makefile_text.splitlines():
        if raw.startswith("\t") or raw.startswith("    "):
            if current is not None:
                buf.append(raw.strip())
            continue
        # 顶层行 —— 先收尾上一个目标
        if current is not None:
            recipes[current] = "\n".join(buf)
            current, buf = None, []
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" in stripped.split(":")[0]:
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", stripped)
        if match:
            current, buf = match.group(1), []
    if current is not None:
        recipes[current] = "\n".join(buf)
    return recipes


def _target_comments(makefile_text: str) -> dict[str, str]:
    """把 Makefile 里 `target:  ## 说明` 的说明文字取成 {目标名: 说明}。"""
    out: dict[str, str] = {}
    for line in makefile_text.splitlines():
        if line.startswith(("\t", "    ")):
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*:.*?##\s*(.*)$", line.strip())
        if match:
            out[match.group(1)] = match.group(2).strip()
    return out


def _find_bad_invocations(makefile_text: str) -> list[str]:
    """返回所有「exec … python scripts/x.py」形状的 recipe 行。"""
    return [
        line.strip()
        for line in makefile_text.splitlines()
        if BAD_SCRIPT_INVOCATION.search(line)
    ]


class TestContainerScriptInvocation:
    """Makefile 在容器内跑仓库脚本时，不得踩那三个雷。"""

    # ---------- 扫描器自证 ----------

    def test_scanner_self_proof_positive(self) -> None:
        """★ 自证（正例）：喂一段坏样本，扫描器必须报出来。"""
        bad = (
            "bootstrap-demo-users:\n"
            "\tdocker compose exec -T backend python scripts/bootstrap_demo_users.py\n"
        )
        assert _find_bad_invocations(bad), (
            "扫描器漏报了已知的坏写法 —— 它已经失效，后面两条断言全是空转"
        )

    def test_scanner_self_proof_negative(self) -> None:
        """★ 自证（反例）：注释里的坏样例不该被算作违规。"""
        good = (
            "# 反例（不要这么写）：python scripts/bootstrap_demo_users.py\n"
            "bootstrap-demo-users:\n"
            "\tdocker compose exec -T backend python -m scripts.bootstrap_demo_users\n"
        )
        assert _find_bad_invocations(good) == [], (
            "扫描器把注释里的说明文字当成了真实违规 —— 会制造永远修不好的红"
        )

    # ---------- 主检查 ----------

    def test_no_script_path_invocation_in_container(self, project_root: Path) -> None:
        """★ 主检查：容器内一律 `python -m scripts.x`，不得 `python scripts/x.py`。"""
        text = _read(project_root, MAKEFILE)
        offenders = _find_bad_invocations(text)

        assert not offenders, (
            "Makefile 里存在「容器内按路径执行仓库脚本」的写法：\n  "
            + "\n  ".join(offenders)
            + "\n\n镜像 WORKDIR=/app 且未把 /app 放进 sys.path 时，"
            "\n`python scripts/x.py` 的 sys.path[0] 是 /app/scripts，"
            "\n脚本里的 `from app...` 会直接 ModuleNotFoundError。"
            "\n改用 `python -m scripts.x`（sys.path[0] = CWD = /app）。"
        )

    def test_scanner_reached_the_real_targets(self, project_root: Path) -> None:
        """★ 自证：确认真的解析到了 Makefile 的目标，而不是解析空。"""
        recipes = _recipes(_read(project_root, MAKEFILE))
        assert len(recipes) > 20, (
            f"只解析出 {len(recipes)} 个目标，明显不对 —— 解析器已失效"
        )
        assert "bootstrap-demo-users" in recipes, "解析器漏掉了已知存在的目标"
        assert "prod-bootstrap-demo-users" in recipes, (
            "生产建号入口 prod-bootstrap-demo-users 不见了 —— "
            "交付文档让运维跑的就是它，缺了等于 P0-5 又没有解"
        )

    def test_prod_bootstrap_target_is_correct(self, project_root: Path) -> None:
        """★ 定点守门：生产建号入口必须同时满足三个条件。

        少任何一个都会让「最后一米」重新变成死命令：
          · 用 prod compose，否则插值阶段就报 POSTGRES_PASSWORD 缺失；
          · 用 `-m`，否则 ModuleNotFoundError: No module named 'app'；
          · 带 --confirm-production，否则脚本自己的生产闸门 SystemExit。
        """
        recipe = _recipes(_read(project_root, MAKEFILE))["prod-bootstrap-demo-users"]

        assert PROD_COMPOSE_MARKER in recipe, (
            "prod-bootstrap-demo-users 没有用 $(PROD_COMPOSE)。\n"
            "走开发 compose 会在插值阶段失败：\n"
            "  error while interpolating services.postgres.environment.POSTGRES_PASSWORD"
        )
        assert GOOD_SCRIPT_INVOCATION.search(recipe), (
            "prod-bootstrap-demo-users 必须写成 `python -m scripts.bootstrap_demo_users`"
        )
        assert "--confirm-production" in recipe, (
            "少了 --confirm-production：APP_ENV=production 时脚本会直接 SystemExit，\n"
            "这个闸门是有意设计的（防止误改生产用户表），不能靠去掉它来绕过。"
        )

    def test_dev_bootstrap_target_uses_module_form(self, project_root: Path) -> None:
        """开发入口同样要写成 `-m`，否则本地也复现同一个 ModuleNotFoundError。"""
        recipe = _recipes(_read(project_root, MAKEFILE))["bootstrap-demo-users"]
        assert GOOD_SCRIPT_INVOCATION.search(recipe), (
            "bootstrap-demo-users 必须写成 `python -m scripts.bootstrap_demo_users`"
        )
        assert "--confirm-production" not in recipe, (
            "开发入口不该带 --confirm-production —— 那个闸门只为生产而设，\n"
            "带上会让「本地为什么跑不通」变成一个假问题。"
        )

    def test_dockerfile_declares_pythonpath(self, project_root: Path) -> None:
        """★ 根因守卫：镜像要把仓库根放进 sys.path。

        这一行是「按路径执行」也能工作的前提；即使有人手敲
        `docker compose exec backend python scripts/create_user.py` 也不该炸。
        """
        text = _read(project_root, BACKEND_DOCKERFILE)
        assert re.search(r"PYTHONPATH\s*=\s*/app", text), (
            "backend/Dockerfile 里没有 PYTHONPATH=/app。\n"
            "没有它，任何 `python scripts/x.py` 形式的调用都会\n"
            "ModuleNotFoundError: No module named 'app'。"
        )

    def test_no_prod_named_target_runs_demo_bootstrap_without_confirm(
        self, project_root: Path
    ) -> None:
        """任何用 prod compose 跑建号脚本的目标，都必须带 --confirm-production。"""
        recipes = _recipes(_read(project_root, MAKEFILE))
        offenders = [
            name
            for name, body in recipes.items()
            if PROD_COMPOSE_MARKER in body
            and "bootstrap_demo_users" in body
            and "--confirm-production" not in body
        ]
        assert not offenders, (
            "以下目标用生产 compose 跑建号脚本却没带 --confirm-production，"
            "运行到脚本内闸门会 SystemExit：\n  " + "\n  ".join(offenders)
        )


class TestDevProdTargetPairs:
    """有生产孪生目标的开发目标，必须在 `make help` 里自报「开发环境」。

    ★ 为什么需要这条
    ────────────────
    2026-09-27 在服务器上连踩两次同形状的坑，都是「挑错了孪生目标」：

    ```
    make bootstrap-demo-users   → 插值阶段就报 POSTGRES_PASSWORD 缺失
    make ensure-partitions      → 同上（且 /sql 挂载点只在 prod compose 里）
    ```

    两次的诱因一模一样：**`make help` 里的说明文字没写环境**。
    运维在服务器上执行 `make help`，看到「补建 t_track 分区（幂等，可随时执行）」
    这种措辞，没有任何理由怀疑它只能在开发机上跑。

    所以约束是：开发目标的两点必须在说明里讲清楚 ——
      ① 标题里出现「开发」；
      ② 正文或标题里点名生产孪生目标。
    否则运维只能靠读 Makefile 源码才能挑对。
    """

    # 开发目标 -> 生产孪生目标
    PAIRS = {
        "bootstrap-demo-users": "prod-bootstrap-demo-users",
        "ensure-partitions": "prod-track-partitions",
    }

    def test_each_dev_target_has_a_prod_twin(self, project_root: Path) -> None:
        text = _read(project_root, MAKEFILE)
        recipes = _recipes(text)
        comments = _target_comments(text)

        for dev, prod in self.PAIRS.items():
            assert dev in recipes, f"开发目标 {dev} 不见了"
            assert prod in recipes, (
                f"{dev} 的生产孪生 {prod} 不存在 —— 服务器上就没有可用入口了"
            )
            assert PROD_COMPOSE_MARKER in recipes[prod], (
                f"{prod} 没有用 $(PROD_COMPOSE)，在服务器上会走开发 compose 而失败"
            )

    def test_dev_targets_declare_their_environment(self, project_root: Path) -> None:
        text = _read(project_root, MAKEFILE)
        comments = _target_comments(text)

        offenders: list[str] = []
        for dev, prod in self.PAIRS.items():
            comment = comments.get(dev, "")
            if "开发" not in comment or prod not in comment:
                offenders.append(
                    f"{dev}: {comment!r} —— 需同时含「开发」与生产孪生名 {prod!r}"
                )
        assert not offenders, (
            "以下开发目标的 `##` 说明没交代环境，运维看 `make help` 会挑错：\n  "
            + "\n  ".join(offenders)
        )

    def test_scanner_self_proof(self) -> None:
        """★ 自证：注释解析器真的能取到 `##` 说明。"""
        sample = "bootstrap-demo-users:  ## 开发环境：写入演示账号\n\tdocker compose x\n"
        assert _target_comments(sample) == {
            "bootstrap-demo-users": "开发环境：写入演示账号"
        }, "注释解析器失效 —— 上面的检查会变成空转"


class TestHostPythonInvocation:
    """宿主机侧的 `python` 必须走 $(PYTHON)，不能裸调。

    ★ 为什么需要这条
    ────────────────
    实测缺陷（2026-09-27，生产服务器 /opt/seasight）：

    ```
    $ make check-demo-accounts PROBE_URL=https://seasight.example.com/seasight
    /bin/bash: line 1: python: command not found
    make: *** [Makefile:174: check-demo-accounts] Error 127
    ```

    Ubuntu 服务器上**没有 `python` 这个可执行文件**，只有 `python3`。
    而 Windows 开发机上通常只有 `python`。于是同一份 Makefile 里
    23 个 target 属于「在我机器上好好的」—— 恰恰是交付文档里让运维
    照着敲的那几条（`check-demo-accounts` / `deploy-verify` /
    `check-demo-approval`）在服务器上全是 127。

    正确做法是把解释器抽成 `PYTHON ?=` 变量并在 recipe 里引用它，
    由 `command -v python3` 决定。

    ★ 注意区分：容器内的 `docker compose exec backend python ...`
    用的是**镜像里**的解释器，不属于本条管辖，不要改成 $(PYTHON)。
    """

    HOST_BARE_PYTHON = re.compile(r"^\t@?python ", re.MULTILINE)

    def test_scanner_self_proof_positive(self) -> None:
        """★ 自证（正例）：坏样本必须被报出来。"""
        bad = "check-api:\n\tpython scripts/check_api_contract.py\n"
        assert self.HOST_BARE_PYTHON.findall(bad), "扫描器漏报了裸 python 调用"

    def test_scanner_self_proof_negative(self) -> None:
        """★ 自证（反例）：容器内调用与注释不该被误报。"""
        good = (
            "bootstrap-demo-users:\n"
            "\tdocker compose exec -T backend python -m scripts.bootstrap_demo_users\n"
            "# 说明文字：服务器上没有 python，只有 python3\n"
        )
        assert self.HOST_BARE_PYTHON.findall(good) == [], (
            "扫描器把容器内调用或注释误判成宿主机裸调用"
        )

    def test_python_variable_is_declared(self, project_root: Path) -> None:
        """PYTHON 变量必须存在，且是「探测 python3 优先」的形式。

        ★ 这里刻意用 `^PYTHON ?=` 锚定行首：本项目另有一个
        `NEXENT_PYTHON ?=`，用 `in` 判子串会被它误命中 ——
        本守卫的作者就在这一点上翻过一次车（变量根本没插进去，却以为插了）。
        """
        text = _read(project_root, MAKEFILE)
        assert re.search(r"(?m)^PYTHON\s*\?=", text), (
            "Makefile 里没有行首定义的 `PYTHON ?=`。\n"
            "★ 用子串判断会误命中 NEXENT_PYTHON，务必按行首锚定。"
        )
        assert "command -v python3" in text, (
            "PYTHON 变量没有探测 python3 —— 服务器上只有 python3，"
            "写死 `PYTHON ?= python` 等于没修"
        )

    def test_no_bare_host_python(self, project_root: Path) -> None:
        """★ 主检查：recipe 行首不得出现裸 `python `。"""
        offenders = self.HOST_BARE_PYTHON.findall(_read(project_root, MAKEFILE))
        assert not offenders, (
            f"Makefile 里有 {len(offenders)} 处 recipe 以裸 `python ` 开头。\n"
            "Ubuntu 服务器上没有 `python`，这些 target 会报 Exit 127。\n"
            "改成 `$(PYTHON)`（容器内的调用除外）。"
        )

    def test_scanner_reached_the_recipes(self, project_root: Path) -> None:
        """★ 自证：确认扫描器真的看到了 recipe 行，不是扫了个空。"""
        text = _read(project_root, MAKEFILE)
        assert re.search(r"(?m)^\t@?\$\(PYTHON\) ", text), (
            "一条 $(PYTHON) 调用都没扫到 —— 扫描器或替换都没生效"
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
