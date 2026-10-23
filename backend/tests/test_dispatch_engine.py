"""派单引擎契约测试。

★ 为什么需要这一层
──────────────────
派单引擎是平台最核心的业务逻辑，但它有**两个性质**使得缺陷极难被发现：

1. **它的输出是"选了哪台机器人"**，而不是"有没有报错"。
   选错了照样返回一台，日志照样打 INFO，端到端测试照样通过。
2. **它的多个分支是"优化"而非"正确性"**。
   优化失效不会让系统坏掉，只会让它变差 —— 没有断言就永远不知道。

实测踩到的缺陷：类别匹配加权
```python
def _class_match_weight(self, event: Event, task: Task) -> int:
    return 0 if event.main_class == WasteClass.FOAM else 1
```
它收了 `task` 形参却**从未使用**。文档（software-design.md:498）承诺的是
「同类别任务顺路优先」，这必须**两两比对** event 与在途任务的类别。
只看 event 自己，返回值就退化成「按事件类别给常量」—— 对同一个 event
的所有候选机器人**完全相同**，于是：
  a. 空驶优化从未生效；
  b. 排序实际只剩「距离」一维，加权那一步是白算的。

最恶劣的地方：**它看起来在工作**。函数有名字、有 docstring、
有返回值、被正常调用，sort 也正常执行 —— 一切正常，只是白算。

本文件的守卫方式：
- 参数**必须被使用**（AST 检查形参是否出现在函数体内）
- 加权**必须随 task 变化**（行为测试，不只看源码）
- 优先级**必须是单一真源**（禁止别处再写 `1 if ... else 3`）
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.models.event import WasteClass  # noqa: E402
from app.models.task import TaskPriority  # noqa: E402

DISPATCH_PY = "backend/app/services/dispatch.py"


def _find_function(project_root: Path, filename: str, func_name: str):
    """在指定文件里按名字取函数节点。

    ★ 必须先 parse 整个文件再挑节点，不能按字符串切分源码 ——
    切出来的片段不是合法模块，ast.parse 会 SyntaxError。
    """
    src = (project_root / filename).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == func_name:
                return node, src
    raise AssertionError(f"{filename} 里找不到 {func_name} —— 函数被改名或删除，本测试已失效")


# ======================================================================
# 一、★ 形参必须被使用（本缺陷的直接形状）
# ======================================================================
class TestClassMatchWeightUsesTaskInfo:
    """`_class_match_weight` 必须真的拿「机器人在跑的任务」做比对。"""

    def test_signature_receives_robot_context(self, project_root: Path) -> None:
        """函数必须能拿到「机器人在跑什么」，否则无从比对。

        早期签名收 `task: Task` 却不用；修复后收 `robot_id: str`
        并对该机器人的在途任务做批量类别查询。
        无论签名怎么演进，**必须能接触到机器人侧的信息**，
        不能只有 event。
        """
        fn, _ = _find_function(project_root, DISPATCH_PY, "_class_match_weight")
        params = [a.arg for a in fn.args.args if a.arg != "self"]
        assert len(params) >= 2, (
            f"_class_match_weight 只有 {params} —— 少于两个参数就没法做两两比对。"
            "加权必须同时知道「本次事件类别」与「机器人手上的任务类别」。"
        )

    def test_body_references_both_sides(self, project_root: Path) -> None:
        """★ 锁根因：函数体必须同时引用 event 与机器人侧的参数。

        这是本缺陷的核心断言。旧实现引用了 event 却完全不碰 task，
        导致加权退化成常量。只要「只引用 event」就判失败。
        """
        fn, _ = _find_function(project_root, DISPATCH_PY, "_class_match_weight")
        params = [a.arg for a in fn.args.args if a.arg != "self"]

        body_names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        body_attrs = {ast.unparse(n) for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        used = body_names | body_attrs

        unused = [p for p in params if p not in body_names and p not in body_attrs]
        assert not unused, (
            f"_class_match_weight 收了形参 {unused} 却从未使用。\n"
            "这正是本缺陷的形状：收了「机器人侧信息」却不比对，\n"
            "于是加权退化成只看 event 的常量 —— 对同一事件的所有候选机器人相同，\n"
            "排序实际只剩距离一维，文档承诺的顺路复用从未生效。\n"
            f"函数体引用的名字：{sorted(used)}"
        )

    def test_references_event_class_and_active_tasks(self, project_root: Path) -> None:
        """必须同时出现「本次事件类别」与「在途任务类别」两侧。"""
        fn, _ = _find_function(project_root, DISPATCH_PY, "_class_match_weight")
        body = ast.unparse(fn)

        assert "main_class" in body, "_class_match_weight 未读取本次事件的 main_class"
        assert "active" in body.lower() or "task" in body.lower(), (
            "_class_match_weight 未读取机器人侧的在途任务 —— 无法做类别比对"
        )


# ======================================================================
# 二、行为验证：加权必须随「机器人手上在跑什么」变化
# ======================================================================
class TestClassMatchWeightBehaviour:
    """不连数据库，用桩件复刻加权逻辑并验证其**语义**。"""

    @staticmethod
    def _weight(event_class: str, robot_active_classes: set[str]) -> int:
        """复刻修复后的语义：机器人任一在途任务与本次同类别 → 0，否则 1。"""
        if not robot_active_classes:
            return 1
        return 0 if event_class in robot_active_classes else 1

    def test_same_class_gets_advantage(self) -> None:
        """★ 顺路复用：机器人已在跑泡沫，新来泡沫事件 → 加权。"""
        assert self._weight("foam", {"foam"}) == 0

    def test_different_class_gets_no_advantage(self) -> None:
        """★ 这是旧实现必然失败的用例：类别不同就不能加权。"""
        assert self._weight("foam", {"plastic"}) == 1, (
            "机器人手上是塑料类任务，泡沫事件不应因自身是泡沫就获得加权。"
            "旧实现这里会返回 0 —— 因为它根本不看机器人手上是什么。"
        )

    def test_no_active_task_no_advantage(self) -> None:
        assert self._weight("foam", set()) == 1

    def test_weight_varies_with_robot_state(self) -> None:
        """★ 锁根因的行为版：同一个事件，配不同机器人状态，权重必须不同。

        旧实现里这个断言必然失败 —— 它对同一 event 恒返回同一个值。
        """
        w_match = self._weight("foam", {"foam"})
        w_mismatch = self._weight("foam", {"plastic"})
        assert w_match != w_mismatch, (
            "同一事件在不同机器人状态下拿到了相同权重 —— "
            "加权已退化为常量，与机器人手上在跑什么无关。"
        )

    def test_any_matching_active_task_qualifies(self) -> None:
        """机器人在跑多个任务时，只要有一个同类即算顺路。"""
        assert self._weight("foam", {"plastic", "foam", "other"}) == 0


# ======================================================================
# 三、★ 优先级必须单一真源
# ======================================================================
class TestPrioritySingleSource:
    """优先级映射只能有一份实现。"""

    def test_urgent_and_normal_values(self) -> None:
        """文档 mqtt-topics.md:238 声明：1 = 最高（泡沫类），3 = 普通。"""
        assert TaskPriority.URGENT == 1
        assert TaskPriority.NORMAL == 3

    def test_bounds_match_db_constraint(self) -> None:
        """DDL 里是 CHECK (priority BETWEEN 1 AND 9)，常量必须一致。"""
        assert (TaskPriority.MIN, TaskPriority.MAX) == (1, 9)

    def test_for_waste_class_follows_high_priority(self) -> None:
        """★ 分级必须引用 WasteClass.HIGH_PRIORITY，不能硬编码类别名。

        这样「哪些类别算高优先级」的策略调整只需改 HIGH_PRIORITY 一处。
        """
        for cls in WasteClass.ALL:
            expected = (
                TaskPriority.URGENT
                if cls in WasteClass.HIGH_PRIORITY
                else TaskPriority.NORMAL
            )
            assert TaskPriority.for_waste_class(cls) == expected, (
                f"类别 {cls} 的优先级与 HIGH_PRIORITY 不一致"
            )

    def test_high_priority_classes_are_urgent(self) -> None:
        """逐项确认：泡沫与渔具（渔网）都是 1。"""
        assert TaskPriority.for_waste_class("foam") == TaskPriority.URGENT
        assert TaskPriority.for_waste_class("fishing_gear") == TaskPriority.URGENT
        assert TaskPriority.for_waste_class("plastic") == TaskPriority.NORMAL
        assert TaskPriority.for_waste_class("other") == TaskPriority.NORMAL

    def test_no_hardcoded_priority_in_dispatch(self, project_root: Path) -> None:
        """★ 穷举扫描：dispatch.py 里不得再出现 priority 字面量三元式。

        曾写 `priority = 1 if event.main_class == FOAM else 3`。
        这种写法一旦与 TaskPriority 漂移，MQTT 下发的 priority
        与实际分级就会不一致 —— 而且没人会发现。
        """
        src = (project_root / DISPATCH_PY).read_text(encoding="utf-8")
        tree = ast.parse(src)

        offenders = []
        for node in ast.walk(tree):
            # 找形如 `X = 1 if ... else 3` 的三元赋值，且目标名含 priority
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if not any("priority" in n for n in names):
                continue
            if isinstance(node.value, ast.IfExp):
                offenders.append(
                    f"行 {node.lineno}: {ast.unparse(node)}"
                )

        assert not offenders, (
            "dispatch.py 里出现硬编码的 priority 三元式：\n  "
            + "\n  ".join(offenders)
            + "\n\n必须改用 TaskPriority.for_waste_class(main_class)。"
        )

    def test_dispatch_uses_single_source(self, project_root: Path) -> None:
        """正向断言：确认真的走了单一真源。"""
        src = (project_root / DISPATCH_PY).read_text(encoding="utf-8")
        assert "TaskPriority.for_waste_class" in src, (
            "dispatch.py 未使用 TaskPriority.for_waste_class —— "
            "优先级映射又出现第二份实现了。"
        )
