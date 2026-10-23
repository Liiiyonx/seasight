"""静态守卫：`check_demo_approval.py` 的「开发环境项」不得在远端目标上判失败。

★ 为什么需要这条
────────────────
`scripts/check_demo_approval.py` 的静态自检里有两条**只描述开发/演示环境**的检查：

  ① 仓库根 `.env` 是否存在、`AGENT_REQUIRE_APPROVAL_FOR_WRITE` 是否为 true；
  ③ 经 compose 插值后 backend 容器实际会拿到什么值。

生产服务器上这两条**本就不成立、且不该成立**：服务器只有 `.env.production`；
生产编排刻意**不**透传 `AGENT_*` 开关（与「生产默认与生产策略一致」的说法相符），
审批高光只在本地/演示机录。

不区分目标的话，在服务器上跑：

    make check-demo-accounts PROBE_URL=https://seasight.example.com/seasight

会稳定打印两条「未就位」并以 exit 1 结束 —— 而四账号真实登录其实全 ✅。
2026-09-27 实测就是这个结果。运维看到「未就位」的直觉反应是去修，
而这条提示会把人引向**去生产打开审批开关**，那恰好是文档明确禁止的事。

所以：远端目标下，①③ 必须降级为「仅参考」，且不参与退出码。
本守卫同时钉住「降级机制存在」与「判定函数正确」两件事。
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

SCRIPT_REL = "scripts/check_demo_approval.py"


def _load_module(project_root: Path):
    """把仓库根的脚本当模块加载（它只依赖标准库，import 无副作用）。"""
    path = project_root / SCRIPT_REL
    assert path.is_file(), f"{SCRIPT_REL} 不存在 —— 守卫的前提文件被挪走了"
    spec = importlib.util.spec_from_file_location("_wb_check_demo_approval", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestProbeScope:
    """远端目标下，开发环境专属检查必须降级为信息项。"""

    def test_is_local_target_classifies_correctly(self, project_root: Path) -> None:
        """判定函数必须把「本机」与「服务器」分得开。"""
        mod = _load_module(project_root)

        for local in (
            "http://localhost:8000",
            "http://localhost:8000/seasight",
            "http://127.0.0.1:8000/",
            "https://127.0.0.1/seasight",
        ):
            assert mod.is_local_target(local), f"{local} 应判定为本机目标"

        for remote in (
            "https://seasight.example.com/seasight",
            "https://seasight.example.com/seasight",
            "http://10.0.0.7:8000/seasight",
        ):
            assert not mod.is_local_target(remote), (
                f"{remote} 应判定为远端目标 —— 判错会让生产自检直接红"
            )

    def test_scanner_self_proof(self, project_root: Path) -> None:
        """★ 自证：降级表达式确实能被扫出来（正例）。"""
        bad_sample = ast.parse("(notes if remote_target else problems).append(1)")
        assert _count_degrade_exprs(bad_sample) == 1, (
            "扫描器漏认了标准的降级表达式 —— 下面的主检查是空转"
        )
        # 反例：普通的 if/else 语句不算（必须是三元表达式形态）
        other = ast.parse("if remote_target:\n    notes.append(1)\nelse:\n    problems.append(1)")
        assert _count_degrade_exprs(other) == 0, "扫描器把普通 if 语句误认成降级表达式"

    def test_two_checks_are_downgraded(self, project_root: Path) -> None:
        """★ 主检查：① 与 ③ 两处都必须是 `notes if remote_target else problems`。"""
        tree = ast.parse((project_root / SCRIPT_REL).read_text(encoding="utf-8"))
        found = _count_degrade_exprs(tree)
        assert found >= 2, (
            f"只找到 {found} 处降级表达式，应为 2 处（① 本地 .env、③ compose 透传值）。\n"
            "少了任一，服务器上跑 make check-demo-accounts 都会误报「未就位」并 exit 1。"
        )

    def test_helper_is_wired_into_main(self, project_root: Path) -> None:
        """`is_local_target` 必须真的被 main 用到，不能只是个没人调的函数。"""
        source = (project_root / SCRIPT_REL).read_text(encoding="utf-8")
        tree = ast.parse(source)

        called = any(
            isinstance(node, ast.Name) and node.id == "is_local_target"
            for node in ast.walk(tree)
        )
        assert called, (
            "is_local_target 定义了却没人调用 —— 降级逻辑根本没生效（假修）"
        )
        assert "remote_target" in source, "没有 remote_target 这个判定变量"
        assert "仅参考" in source, (
            "输出里没有「仅参考」这类措辞 —— 降级项会和真问题混在一起打印"
        )


def _count_degrade_exprs(tree: ast.AST) -> int:
    """数出 `(notes if remote_target else problems)` 这种三元表达式的个数。"""
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.IfExp):
            continue
        if not (isinstance(node.test, ast.Name) and node.test.id == "remote_target"):
            continue
        if not (isinstance(node.body, ast.Name) and node.body.id == "notes"):
            continue
        if not (isinstance(node.orelse, ast.Name) and node.orelse.id == "problems"):
            continue
        count += 1
    return count


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
