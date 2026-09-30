"""消费者 PEL（Pending Entries List）回收测试。

★ 为什么需要这一层
──────────────────
`xreadgroup(streams={stream: ">"})` **只读新消息**，永远不返回 PEL 里的条目。
而 `_process_entry` 在「无可用机器人」时刻意**不 ACK**，把消息留在 PEL
等后续重试。两者一叠加：

  - 这些消息**永远不会被重投**（哪怕机器人后来上线了）；
  - PEL 只增不减，且**不受 stream 的 maxlen 裁剪影响**；
  - 功能上被定时补派从数据库兜住了，所以**从外面完全看不出来**。

于是「留着重试」实际变成了「永久滞留 + 内存缓慢泄漏」——
不报错、不打日志，只能靠 `XPENDING` 才看得见。

这是 Redis Streams 最经典的坑：**不 ACK 就必须有人认领。**

★ 缺陷形状：属于「实现了 N 步、只接上了 N-1 步」的近亲 ——
  不是没人调用，而是**流程缺了一半**：有"不 ACK 留待重试"的意图，
  却没有"谁去重试"的实现。

★ 守卫方式：
  - 行为测试：用假 Redis 验证 `_reclaim_pending` 真的调了 `xautoclaim`
    并正确解出 `[start_id, entries, deleted]` 的中间段
  - AST：**断言 `dispatch_consumer` 循环里调用了 `_reclaim_pending`**
  - 锁根因：认领的 `min_idle_ms` 必须 > 0，否则会把「正在处理」的消息
    也抢回来重复处理
"""

from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services import consumer  # noqa: E402

CONSUMER_PY = BACKEND / "app" / "services" / "consumer.py"


class FakeRedis:
    """最小假 Redis：只实现 `xautoclaim`，并记录调用参数。"""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[tuple, dict]] = []

    async def xautoclaim(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.result


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ======================================================================
# 一、行为：真的调了 xautoclaim，且解对了返回结构
# ======================================================================
class TestReclaimPending:

    def test_calls_xautoclaim_with_expected_args(self) -> None:
        fake = FakeRedis([b"0-0", [(b"1-1", {"event_id": "evt_1"})], []])
        run(consumer._reclaim_pending(fake, "stream:x", "grp", "worker-1"))

        assert fake.calls, "没有调用 xautoclaim —— PEL 里的消息永远不会被重试"
        args, kwargs = fake.calls[0]
        assert args[0] == "stream:x"
        assert args[1] == "grp"
        assert args[2] == "worker-1"
        assert args[3] > 0, "min_idle_time 必须 > 0"
        assert kwargs.get("count") == 10

    def test_returns_middle_segment(self) -> None:
        """redis-py 返回 `[start_id, entries, deleted]` —— 取中间那段。

        entry_id 保持**原样回传**（真实 Redis 给的是 bytes），
        与 `xreadgroup` 的行为一致，最终由 `xack` 消费。
        """
        fake = FakeRedis(
            [b"0-0", [(b"1-1", {"event_id": "evt_1"}), (b"1-2", {"event_id": "evt_2"})], []]
        )
        out = run(consumer._reclaim_pending(fake, "s", "g", "c"))
        assert out == [(b"1-1", {"event_id": "evt_1"}), (b"1-2", {"event_id": "evt_2"})]

    def test_empty_pel(self) -> None:
        fake = FakeRedis([b"0-0", [], []])
        assert run(consumer._reclaim_pending(fake, "s", "g", "c")) == []

    def test_none_entries(self) -> None:
        """某些客户端/版本会返回 None 而不是 []。"""
        fake = FakeRedis([b"0-0", None, []])
        assert run(consumer._reclaim_pending(fake, "s", "g", "c")) == []

    @pytest.mark.parametrize("bad", [None, [], [b"0-0"], "unexpected"])
    def test_malformed_result_is_tolerated(self, bad: Any) -> None:
        assert run(consumer._reclaim_pending(FakeRedis(bad), "s", "g", "c")) == []

    def test_default_min_idle_is_positive(self) -> None:
        """锁根因：默认空闲阈值必须 > 0。

        设成 0 会把「刚投递、正在处理」的消息也抢回来 ——
        同一条事件被并发处理两遍。
        """
        assert consumer.PEL_MIN_IDLE_MS > 0


# ======================================================================
# 二、★ 接线：消费者的主循环必须真的认领 PEL
# ======================================================================
class TestReclaimIsWired:
    """`_reclaim_pending` 实现了却没接进循环 = 和没写一样。"""

    def _function_node(self, name: str) -> ast.AST:
        tree = ast.parse(CONSUMER_PY.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == name:
                    return node
        raise AssertionError(
            f"consumer.py 里找不到函数 {name} —— 守卫自身失效，必须修测试锚点"
        )

    @staticmethod
    def _called_names(node: ast.AST) -> list[str]:
        names = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                if isinstance(fn, ast.Name):
                    names.append(fn.id)
                elif isinstance(fn, ast.Attribute):
                    names.append(fn.attr)
        return names

    def test_consumer_loop_reclaims_pel(self) -> None:
        node = self._function_node("dispatch_consumer")
        names = self._called_names(node)
        assert "_reclaim_pending" in names, (
            "dispatch_consumer 的主循环没有调用 _reclaim_pending。\n"
            "`xreadgroup('>')` 永远读不到 PEL，而处理失败的消息又不 ACK ——\n"
            "结果是这些消息**永远不会被重试**，PEL 无限增长且不被 maxlen 裁剪。\n"
            "功能上被定时补派从数据库兜住了，所以外表完全正常。"
        )

    def test_reclaim_uses_xautoclaim(self) -> None:
        """自证：认领函数必须真的用 xautoclaim，而不是退化成读新消息。"""
        node = self._function_node("_reclaim_pending")
        assert "xautoclaim" in self._called_names(node), (
            "_reclaim_pending 没有调用 xautoclaim —— 那它认领不了任何 PEL 条目。"
        )

    def test_scanner_self_proof(self) -> None:
        """自证：扫描器能扫到已知调用，否则上面的'通过'没有意义。"""
        node = self._function_node("dispatch_consumer")
        assert "xreadgroup" in self._called_names(node), (
            "扫描器连 xreadgroup 都扫不到，说明它已经失效。"
        )


# ======================================================================
# 三、补派循环：先快照标量 ID，再逐条使用独立 session
# ======================================================================
class TestPendingDispatcherResilience:
    """锁住真实故障的两层根因。

    1. ``EventStatus.NEW`` 只在循环内引用；漏导入时不会在启动阶段暴露，
       而是每条事件都失败，把可用事件全部留在原地。
    2. ``rollback`` 会过期 ORM 对象。若循环继续从上一轮的 Task/Event
       读取 ID，首个异常会升级成整轮 ``MissingGreenlet``，后续事件不再处理。
    """

    @staticmethod
    def _function_node(name: str) -> ast.AsyncFunctionDef:
        tree = ast.parse(CONSUMER_PY.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
                return node
        raise AssertionError(
            f"consumer.py 里找不到异步函数 {name} —— 守卫自身失效，必须修测试锚点"
        )

    def test_event_status_is_imported_inside_dispatcher(self) -> None:
        node = self._function_node("pending_dispatcher")
        imported = {
            alias.name
            for sub in ast.walk(node)
            if isinstance(sub, ast.ImportFrom) and sub.module == "app.models.event"
            for alias in sub.names
        }
        assert "EventStatus" in imported, (
            "pending_dispatcher 使用了 EventStatus.NEW，但没有导入 EventStatus。\n"
            "该错误只会在每轮补派处理事件时出现，导致所有可派事件持续失败。"
        )

    def test_snapshots_scalar_ids_before_per_item_sessions(self) -> None:
        node = self._function_node("pending_dispatcher")
        expected_attrs = {
            "assigned_ids": "task_id",
            "pending_ids": "task_id",
            "event_ids": "event_id",
        }
        snapshot_lines: dict[str, int] = {}

        for sub in ast.walk(node):
            if not isinstance(sub, (ast.Assign, ast.AnnAssign)):
                continue
            target = sub.targets[0] if isinstance(sub, ast.Assign) else sub.target
            if not isinstance(target, ast.Name) or target.id not in expected_attrs:
                continue
            value = sub.value
            if not isinstance(value, ast.ListComp) or not isinstance(value.elt, ast.Attribute):
                continue
            if value.elt.attr == expected_attrs[target.id]:
                snapshot_lines[target.id] = sub.lineno

        assert set(snapshot_lines) == set(expected_attrs), (
            "pending_dispatcher 必须先把 assigned/pending/event ID 快照成标量列表。\n"
            "直接把 ORM 对象留给逐条 session：一次 rollback 就会过期对象属性，\n"
            "后续读取触发 MissingGreenlet，并中断整轮补派。"
        )

        loop_lines: dict[str, int] = {}
        for sub in ast.walk(node):
            if not isinstance(sub, ast.For) or not isinstance(sub.iter, ast.Name):
                continue
            if sub.iter.id in expected_attrs:
                loop_lines[sub.iter.id] = sub.lineno

        assert set(loop_lines) == set(expected_attrs), (
            "补派循环必须遍历快照后的 ID 列表，不能重新遍历 ORM 对象。"
        )
        for name in expected_attrs:
            assert snapshot_lines[name] < loop_lines[name], (
                f"{name} 的快照必须发生在对应循环之前。"
            )
