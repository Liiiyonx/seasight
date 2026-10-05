"""事件上报接口的响应契约测试。

★ 为什么需要这一层
──────────────────
`POST /events` 的响应体里有两个字段：`task_created` / `task_id`。
api.md:148 明确声明了它们，含义是「本次事件是否派单成功、派给了哪个任务」。

实测踩到的缺陷（探针已复现）：api/v1/events.py 曾经这样写
    tasks = await TaskRepository(session).list_tasks(limit=1)
    if tasks[0]:
        result.task_created = True
        result.task_id = tasks[0][0].task_id
而 `list_tasks` 的返回类型是 `tuple[list[Task], int]`。

这里有**两处叠加**的错误，任何一处单独拿出来都不显眼：
  1. 没解包元组 —— `tasks[0]` 拿到的是 list 而不是 total。
     非空 list 恒为真值，所以判断条件形同虚设。
  2. 反查「全库最新一条任务」冒充「本次调用创建的任务」。

后果分两层，第二层比第一层危险得多：
  - 浅层：只要库里存在任意一条历史任务，`task_created` 就恒为 True。
    无可用机器人时接口照样回答「派单成功」。前端会去轮询一个不存在的进度。
  - 深层：并发上报时 `task_id` 指向的是**别人的**任务。
    前端拿它查 `/tasks/{task_id}`，会看到一个与本次事件毫无关系的状态，
    而且这个错误在任何日志里都不会出现。

根因不是「忘了解包」，是**把「本次调用的产物」当成「可事后反查的东西」**。
task_id 只能由派单函数回传。这条规则在本文件里被固化成测试。

本文件的守卫方式与 test_ws_contract.py 一致：AST 静态扫描 + 契约常量比对，
不依赖数据库，任何人机器上都能跑。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.mqtt.handlers import _try_dispatch
from app.schemas import EventIngestResult

EVENTS_API = "backend/app/api/v1/events.py"


def _ingest_function(project_root: Path) -> ast.AsyncFunctionDef:
    """取出 events.py 里的 ingest_event 函数节点。

    ★ 不能按字符串切分源码：切出来的片段不是合法模块
    （装饰器/缩进都会断），ast.parse 会直接 SyntaxError。
    必须先 parse 整个文件，再按函数名挑节点。
    """
    src = (project_root / EVENTS_API).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "ingest_event":
                return node
    raise AssertionError(
        f"{EVENTS_API} 里找不到 ingest_event —— 接口被改名或删除，本测试已失效"
    )


# ======================================================================
# 一、契约声明本身
# ======================================================================
class TestIngestResultDeclaration:
    """响应体的字段集必须与 api.md 声明一致。"""

    def test_has_documented_fields(self) -> None:
        """api.md:146-152 声明 6 个字段，一个都不能少。"""
        documented = {
            "accepted",
            "event_id",
            "duplicate",
            "task_created",
            "task_id",
            "message",
        }
        actual = set(EventIngestResult.model_fields)
        missing = documented - actual
        assert not missing, (
            f"EventIngestResult 缺字段 {missing}；api.md 的响应示例里有这些字段，"
            f"删字段等于悄悄改契约。"
        )

    def test_task_created_defaults_false(self) -> None:
        """★ 缺省必须是「没派单」。

        默认值给 True 会让「没派出去」这件事变成不可观测的。
        """
        field = EventIngestResult.model_fields["task_created"]
        assert field.default is False, (
            "task_created 默认值必须是 False —— 默认 True 会把「未派单」伪装成成功。"
        )

    def test_task_id_defaults_none(self) -> None:
        field = EventIngestResult.model_fields["task_id"]
        assert field.default is None


# ======================================================================
# 二、派单函数必须回传本次产物
# ======================================================================
class TestTryDispatchReturnsTask:
    """`_try_dispatch` 必须返回本次创建/命中的 Task。

    这是修复的核心：调用方要拿到 task_id，唯一正确的方式是让它回传。
    """

    def test_signature_returns_task_or_none(self) -> None:
        import inspect

        sig = inspect.signature(_try_dispatch)
        ret = sig.return_annotation
        rendered = ret if isinstance(ret, str) else repr(ret)
        assert "Task" in rendered, (
            f"_try_dispatch 的返回注解应含 Task，实际为 {rendered!r}。"
            "回传本次产物是该函数的契约，不能退化成 None。"
        )

    def test_all_exit_paths_return(self, project_root: Path) -> None:
        """★ 穷举 _try_dispatch 的所有 return 语句。

        早期失败路径（事件不存在 / 无机器人 / task 为 None）必须显式
        `return None`，成功路径必须 `return task`。
        若某个分支写成裸 `return`，返回类型就退化成 NoneType，
        调用方 `if task is not None` 会永远为假 —— 静默失效。
        """
        src = (project_root / "backend/app/mqtt/handlers.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        fn = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "_try_dispatch":
                    fn = node
                    break
        assert fn is not None, "handlers.py 里找不到 _try_dispatch —— 函数被改名或删除，测试已失效"

        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        assert returns, "_try_dispatch 没有任何 return 语句"

        bare = [r for r in returns if r.value is None]
        assert not bare, (
            f"_try_dispatch 有 {len(bare)} 处裸 return（行 "
            f"{[r.lineno for r in bare]}）。返回类型是 Task | None，"
            "每个出口都必须显式给出返回值。"
        )

        values = [ast.unparse(r.value) for r in returns]
        assert "Task" not in " ".join(values)
        assert any(v == "None" for v in values), f"缺少 return None 分支：{values}"
        assert any(v == "task" for v in values), (
            f"成功路径必须 return task（本次创建的产物），实际 return 值：{values}"
        )


# ======================================================================
# 三、★ 全工程防线：禁止「反查最新记录冒充本次产物」
# ======================================================================
class TestNoLatestRecordSubstitution:
    """AST 穷举：不得把「查最新一条记录」当作「本次调用的产物」。

    这是本缺陷的**形状**，不是一处笔误。凡是
        x = await repo.list_xxx(...)      # 返回 tuple
        x[0] / x[0][0]                    # 未解包就下标
    都属于此形状。
    """

    @staticmethod
    def _tuple_returning_methods(project_root: Path) -> dict[str, str]:
        """收集仓库层所有返回 tuple[...] 的方法名。"""
        out: dict[str, str] = {}
        repo_dir = project_root / "backend/app/repositories"
        for path in repo_dir.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if node.returns is None:
                    continue
                ret = ast.unparse(node.returns)
                if ret.startswith("tuple["):
                    out[node.name] = ret
        return out

    def test_scanner_finds_tuple_returning_methods(self, project_root: Path) -> None:
        """★ 自证：确认扫描器真的找到了目标方法。

        若仓库层哪天改用 dataclass 或 NamedTuple 作返回值，
        这个扫描器会静默返回空 dict，下面的穷举检查就变成空转。
        """
        methods = self._tuple_returning_methods(project_root)
        assert methods, (
            "扫描器没找到任何返回 tuple 的仓库方法 —— 守卫已失效。"
            "要么仓库层签名变了，要么 AST 遍历写错了。"
        )
        assert "list_tasks" in methods, (
            f"未找到 list_tasks（当前找到 {sorted(methods)}）。"
            "它是本缺陷的当事方法，缺失说明扫描器覆盖不到。"
        )

    def test_no_undecomposed_tuple_subscript(self, project_root: Path) -> None:
        """穷举全部调用点：tuple 返回被赋值给单个变量后又下标访问的，一律报出。"""
        tuple_methods = self._tuple_returning_methods(project_root)
        offenders: list[str] = []

        for path in (project_root / "backend/app").rglob("*.py"):
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
            lines = src.splitlines()

            # 变量名 -> 赋值行号（仅记录「未解包」的情况）
            suspect_vars: dict[str, int] = {}
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not isinstance(value, ast.Await):
                    continue
                call = value.value
                if not isinstance(call, ast.Call):
                    continue
                fn = call.func
                if not isinstance(fn, ast.Attribute) or fn.attr not in tuple_methods:
                    continue
                for t in targets:
                    if isinstance(t, ast.Tuple):
                        continue  # 正确写法：解包
                    if isinstance(t, ast.Name):
                        suspect_vars[t.id] = node.lineno

            # 这些变量若被下标访问，即为「未解包 + 下标」
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in suspect_vars
                ):
                    continue
                rel = path.relative_to(project_root).as_posix()
                offenders.append(
                    f"{rel}:{node.lineno} `{lines[node.lineno - 1].strip()}` "
                    f"(变量 {node.value.id} 来自第 {suspect_vars[node.value.id]} 行，"
                    f"方法返回 tuple 但未解包)"
                )

        assert not offenders, (
            "发现「元组返回被当标量用」的调用点：\n  "
            + "\n  ".join(offenders)
            + "\n\n正确写法：`items, total = await repo.list_xxx(...)`。"
        )

    def test_events_api_does_not_query_latest_task(self, project_root: Path) -> None:
        """★ 定点守门：/events 上报接口不得出现 list_tasks 反查。

        这是曾出过缺陷的确切位置，单独钉一颗钉子，
        使得「回退到旧写法」会在测试里立刻变红。
        """
        ingest = _ingest_function(project_root)

        bad = []
        for node in ast.walk(ingest):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Attribute) and fn.attr in {
                    "list_tasks",
                    "list_events",
                }:
                    bad.append(f"行 {node.lineno}: {ast.unparse(node)}")

        assert not bad, (
            "ingest_event 里出现了列表反查：\n  " + "\n  ".join(bad)
            + "\n\ntask_id 必须取 _try_dispatch 的返回值，不能事后查库。"
        )

    def test_ingest_uses_dispatch_return_value(self, project_root: Path) -> None:
        """★ 正向断言：确认 _try_dispatch 的返回值**确实被用了**。

        只断言「没有旧写法」是不够的 —— 有人可能把调用改成
        `await _try_dispatch(...)` 丢掉返回值，再手工置 task_created。
        那种写法同样静默：task_id 永远为 None。
        所以这里必须断言返回值被赋给了变量。
        """
        ingest = _ingest_function(project_root)

        found = False
        for node in ast.walk(ingest):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if not isinstance(value, ast.Await):
                continue
            call = value.value
            if not isinstance(call, ast.Call):
                continue
            fn = call.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name == "_try_dispatch":
                found = True
                break

        assert found, (
            "ingest_event 里没有把 _try_dispatch 的返回值赋给变量。"
            "丢掉返回值意味着 task_id 永远是 None，而 task_created 可能会被"
            "手工置 True —— 那又回到了「报告一个不存在的任务」。"
        )


# ======================================================================
# 四、行为验证（不连数据库，用桩件复刻真实解包）
# ======================================================================
class TestUnpackSemantics:
    """把「元组被当标量」的后果固化成可执行的证据。

    这些测试不测生产代码，而是测**历史缺陷的语义**：
    如果有人重新引入旧写法，这些断言说明了会发生什么。
    """

    def test_truthiness_of_tuple_is_always_true(self) -> None:
        """★ 根因：非空 tuple 恒为真值，所以 `if tasks[0]` 判断无效。"""
        empty_result = ([], 0)
        assert bool(empty_result) is True, "空结果集的 tuple 仍为真值"

        populated = (["any-task"], 1)
        assert bool(populated) is True

    def test_indexing_unpacked_tuple_leaks_unrelated_record(self) -> None:
        """★ 后果：下标取到的是「别人的」任务。"""
        unrelated = object()
        result = ([unrelated], 1)  # 库里恰好有一条无关任务
        assert result[0][0] is unrelated, (
            "未解包时 result[0][0] 取到的是列表首元素（与本次调用无关），"
            "而非本次产物 —— 这正是 task_id 张冠李戴的机制。"
        )

    def test_correct_unpacking_separates_items_and_total(self) -> None:
        """对照：正确解包后能区分数据与总数。"""
        items, total = ([1, 2, 3], 3)
        assert items == [1, 2, 3]
        assert total == 3


# ======================================================================
# 五、同步派单入口不得重复入队 Redis Stream
# ======================================================================
class TestNoQueueForSynchronousDispatch:
    """同步派单入口不能同时把事件投进 Redis Stream。

    守的缺陷：HTTP 入口在 `ingest` 里投 Stream，提交后又同步
    `_try_dispatch`；`dispatch_consumer` 再消费一次，同一条事件被
    两个事务同时派单，撞 `uq_task_active_event` 唯一约束返回 500。
    """

    @staticmethod
    def _call_has_enqueue_false(node: ast.Call) -> bool:
        return any(
            kw.arg == "enqueue_dispatch"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is False
            for kw in node.keywords
        )

    def test_http_ingest_disables_stream_enqueue(self, project_root: Path) -> None:
        ingest = _ingest_function(project_root)
        calls = [
            node
            for node in ast.walk(ingest)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "ingest"
        ]
        assert calls, "ingest_event 里找不到 service.ingest 调用"
        assert all(self._call_has_enqueue_false(c) for c in calls), (
            "HTTP 同步派单入口的 ingest 必须传 enqueue_dispatch=False，"
            "否则事件会同时进 Redis Stream 被消费者重复派单。"
        )

    def test_mqtt_ingest_disables_stream_enqueue(self, project_root: Path) -> None:
        src = (project_root / "backend/app/mqtt/handlers.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(src)
        fn = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "handle_event"
            ),
            None,
        )
        assert fn is not None, "handlers.py 里找不到 handle_event"
        calls = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "ingest"
        ]
        assert calls, "handle_event 里找不到 service.ingest 调用"
        assert all(self._call_has_enqueue_false(c) for c in calls), (
            "MQTT 同步派单入口的 ingest 必须传 enqueue_dispatch=False，"
            "否则事件会同时进 Redis Stream 被消费者重复派单。"
        )
