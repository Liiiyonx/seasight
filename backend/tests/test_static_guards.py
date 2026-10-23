"""静态质量守卫：函数收到形参却从不使用。

★ 为什么需要这条
────────────────
实测缺陷（services/dispatch.py）：

```python
def _class_match_weight(self, event: Event, task: Task) -> int:
    return 0 if event.main_class == WasteClass.FOAM else 1
```

它收了 `task` 形参却**从未使用**。于是文档承诺的「同类别顺路优先」
从未生效 —— 返回值对同一事件的所有候选机器人完全相同，
排序实际只剩距离一维。

这个形状的恶劣之处：**函数看起来完全正常**。有名字、有 docstring、
有返回值、被正常调用、不报错、不打日志。唯一的破绽就是
「那个形参在函数体里一次都没出现过」。

而「收了参数不用」往往不是笔误，是**语义漂移的证据**：
调用方以为你在比对，实际你只是返回了一个常量。

本守卫用 AST 穷举全工程的函数定义，报出所有未使用的形参。
允许清单只放三类**明确无害**的约定。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_DIR = "backend/app"

# ----------------------------------------------------------------------
# 允许「收而不用」的形参名 —— 只放明确的框架约定，不放业务名字
# ----------------------------------------------------------------------
ALLOWED_UNUSED: frozenset[str] = frozenset(
    {
        "self",
        "cls",
        # FastAPI lifespan 约定：签名固定为 (app)，但实现未必读它
        "app",
        # 异常处理器约定：签名固定为 (request, exc)
        "request",
        "exc",
        # MQTT handler 统一签名 (topic, payload)：有的 handler
        # 从 payload 里就能拿到标识，不需要解析 topic
        "topic",
        # 显式占位
        "kwargs",
        "args",
    }
)


def _iter_functions(project_root: Path):
    """产出 (相对路径, 函数节点)。"""
    root = project_root / APP_DIR
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield path.relative_to(project_root).as_posix(), node


def _unused_params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """返回该函数未被引用的形参名列表。"""
    a = fn.args
    params: list[str] = []
    for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
        params.append(arg.arg)
    if a.vararg:
        params.append(a.vararg.arg)
    if a.kwarg:
        params.append(a.kwarg.arg)

    body_names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    body_attrs = {ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    used = body_names | body_attrs

    return [p for p in params if p not in used]


class TestNoSilentlyUnusedParameters:
    """穷举全部函数定义，报出「收而不用」的形参。"""

    def test_scanner_finds_functions(self, project_root: Path) -> None:
        """★ 自证：确认扫描器真的走进了源码。

        若 AST 遍历写错（例如漏掉 `ast.AsyncFunctionDef`，
        本项目 5 个 MQTT handler 全是 async），扫描会静默返回空，
        整个守卫变成空转 —— 这正是本项目踩过的坑。
        """
        funcs = list(_iter_functions(project_root))
        assert funcs, f"{APP_DIR} 下一个函数都没扫到 —— 扫描器已失效"

        async_count = sum(
            1 for _, n in funcs if isinstance(n, ast.AsyncFunctionDef)
        )
        assert async_count > 0, (
            "一个 async 函数都没扫到 —— 大概率漏了 ast.AsyncFunctionDef。"
            "本项目 MQTT 层的 handler 全是 async，漏掉等于整个协议层不在覆盖面内。"
        )

    def test_no_unused_parameters(self, project_root: Path) -> None:
        """★ 主检查：任何非允许清单内的未使用形参都要报出。"""
        offenders: list[str] = []

        for rel, fn in _iter_functions(project_root):
            for p in _unused_params(fn):
                if p in ALLOWED_UNUSED or p.startswith("_"):
                    continue
                offenders.append(
                    f"{rel}:{fn.lineno} {fn.name}() 的形参 {p!r} 从未使用"
                )

        assert not offenders, (
            "发现「收了形参却从不使用」的函数：\n  "
            + "\n  ".join(offenders)
            + "\n\n这类写法往往意味着语义漂移：调用方以为你在用它做判断，"
            "\n实际你只是返回了一个与该参数无关的常量（参见 dispatch.py 的"
            "\n_class_match_weight 历史缺陷）。若确为接口约定，"
            "\n请把参数名加入 ALLOWED_UNUSED 并写明理由。"
        )

    def test_class_match_weight_specifically(self, project_root: Path) -> None:
        """★ 定点守门：钉住那个曾出过缺陷的函数。

        即使有人把 robot_id 加进允许清单，这条也会红。
        """
        target = "backend/app/services/dispatch.py"
        for rel, fn in _iter_functions(project_root):
            if rel != target or fn.name != "_class_match_weight":
                continue
            unused = _unused_params(fn)
            assert not unused, (
                f"_class_match_weight 有未使用形参 {unused}。\n"
                "该函数历史缺陷就是「收了 task 却不用」导致顺路复用失效，"
                "\n它不能出现在允许清单里 —— 必须真的做两两类别比对。"
            )
            return
        raise AssertionError(
            f"{target} 里找不到 _class_match_weight —— 函数被改名，本守门已失效"
        )
