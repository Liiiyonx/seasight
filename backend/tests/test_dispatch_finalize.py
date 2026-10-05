"""派单「收尾」契约测试 —— 防止「建了任务却没人通知机器人」。

★ 为什么需要这一层
──────────────────
派单不是一个动作，是**三步**：

    1. 在数据库里创建任务
    2. 通过 MQTT 把指令下发给机器人
    3. 通过 WebSocket 把新任务推给看板

本工程有**四个**派单入口，而实测只有两个走完了三步：

| 入口 | 建任务 | 下发 MQTT | 推送看板 |
| --- | --- | --- | --- |
| `handlers._try_dispatch`（HTTP 上报同步派单） | ✓ | ✓ | ✓ |
| `consumer._process_entry`（Streams 消费者） | ✓ | ✓ | ✓ |
| `consumer.pending_dispatcher`（定时补派） | ✓ | ✗ | ✗ |
| `dispatch.assign_pending_tasks`（补派 API） | ✓ | ✗ | ✗ |

后两个的后果极其隐蔽：
    - 数据库里**确实有**任务记录；
    - 日志写着「本轮补派 N 个任务」；
    - API 返回 `{"dispatched": N, "task_ids": [...]}` 且 HTTP 200；
    - 而机器人**一条指令都没收到**，看板也不刷新，任务永远停在 assigned。

全过程不报错、不抛异常、不打日志 —— 从外面看一切成功。

★ 这条缺陷与前面几轮的形状不同，值得单列：
    前面几轮是「声明了 N 项、实现了 N-1 项」；
    本轮是「**实现了 N 步、只接上了 N-2 步**」。
    代码本身没错，错在**没有接线** —— 而"没接线"是 grep 不出来的，
    因为被调用的函数确实存在于源码里。

★ 修法（锁根因，不打补丁）：
    把收尾动作收成 `dispatch.finalize_dispatch(task)` 一个函数，
    四个入口全部调用它。这样新加入口不可能再各写各的。

★ 守卫方式（本文件）：
    - AST **穷举**所有 `dispatch_for_event` 调用点，
      断言其所在函数体内调用了 `finalize_dispatch`（新入口自动纳入覆盖面）
    - 自证断言：必须扫到 4 个调用点，且必须包含**曾经漏接的那两个**
      （扫不到就说明扫描器失效了，而不是"没问题"）
    - `finalize_dispatch` 体内必须同时出现 `publish_task` 与 `push_task_update`
    - `handle_ack_timeout` 必须有**外部调用点**（它曾经零调用，
      于是文档里「五步筛选」的第五步从未生效）
"""

from __future__ import annotations

import asyncio
import ast
import struct
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.models.task import Task  # noqa: E402
from app.services.dispatch import DispatchEngine, _parse_point  # noqa: E402

APP_DIR = BACKEND / "app"

# 曾经漏接收尾的两个入口 —— 必须出现在扫描结果里，否则说明扫描器失效
ONCE_BROKEN_ENTRY_POINTS = {"pending_dispatcher", "assign_pending_tasks"}


# ======================================================================
# 工具：AST 扫描
# ======================================================================
def iter_python_files() -> list[Path]:
    return sorted(APP_DIR.rglob("*.py"))


def enclosing_functions(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """返回 (函数名, 函数节点) 列表，含模块级的 <module>。"""
    out: list[tuple[str, ast.AST]] = [("<module>", tree)]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((node.name, node))
    return out


def collect_calls(node: ast.AST) -> list[str]:
    """收集一个节点子树内所有被调用的函数名（含 `x.method()` 的 method）。"""
    names: list[str] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fn = sub.func
        if isinstance(fn, ast.Name):
            names.append(fn.id)
        elif isinstance(fn, ast.Attribute):
            names.append(fn.attr)
    return names


def make_task(**overrides: object) -> Task:
    """构造一个内存中的 Task（不需要数据库）。"""
    fields: dict[str, object] = {
        "task_id": "tsk_test_001",
        "event_id": "evt_test_001",
        "robot_id": "robot_A",
        "target_location": "SRID=4326;POINT(120.5 26.1)",
        "status": "assigned",
        "priority": 1,
    }
    fields.update(overrides)
    return Task(**fields)  # type: ignore[arg-type]


# ======================================================================
# 一、坐标解析（不再为取坐标跑一次数据库）
# ======================================================================
class TestParsePoint:
    """`_parse_point` 必须能解析工程里实际出现的两种几何形态。"""

    def test_ewkt_with_srid(self) -> None:
        assert _parse_point("SRID=4326;POINT(120.5 26.1)") == (120.5, 26.1)

    def test_ewkt_without_srid(self) -> None:
        assert _parse_point("POINT(120.5 26.1)") == (120.5, 26.1)

    def test_ewkt_negative_coords(self) -> None:
        assert _parse_point("SRID=4326;POINT(-120.5 -26.1)") == (-120.5, -26.1)

    def test_ewkb_little_endian_with_srid(self) -> None:
        # 小端 + EWKB SRID 标志位（0x20000000）
        data = struct.pack("<BIIdd", 1, 0x20000001, 4326, 120.5, 26.1)
        assert _parse_point(data) == pytest.approx((120.5, 26.1))

    def test_wkb_big_endian_without_srid(self) -> None:
        data = struct.pack(">BIdd", 0, 1, 120.5, 26.1)
        assert _parse_point(data) == pytest.approx((120.5, 26.1))

    def test_wkb_element_like_object(self) -> None:
        """geoalchemy2 的 WKBElement：值在 `.data` 里。"""

        class FakeWKB:
            data = struct.pack("<BIIdd", 1, 0x20000001, 4326, 119.3, 25.7)

        assert _parse_point(FakeWKB()) == pytest.approx((119.3, 25.7))

    def test_non_point_geometry_returns_none(self) -> None:
        """非 Point 不能硬解 —— 返回 None，由调用方留痕。"""
        data = struct.pack("<BI", 1, 2)   # 2 = LineString
        assert _parse_point(data) is None

    @pytest.mark.parametrize("bad", [None, "", "not a geometry", 12345, b""])
    def test_unparseable_returns_none(self, bad: object) -> None:
        assert _parse_point(bad) is None


# ======================================================================
# 二、收尾动作的行为：下发 + 推送，一个都不能少
# ======================================================================
class TestFinalizeDispatchBehavior:

    def test_publishes_and_pushes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.mqtt import client as mqtt_mod
        from app.services import dispatch as dispatch_mod
        from app.ws import manager as ws_mod

        published: list[dict] = []
        pushed: list[dict] = []

        async def fake_publish(**kwargs: object) -> bool:
            published.append(kwargs)
            return True

        async def fake_push(data: dict) -> None:
            pushed.append(data)

        monkeypatch.setattr(mqtt_mod.mqtt_client, "publish_task", fake_publish)
        monkeypatch.setattr(ws_mod.ws_manager, "push_task_update", fake_push)

        task = make_task()
        asyncio.run(dispatch_mod.finalize_dispatch(task))

        assert len(published) == 1, "机器人没收到指令 —— 补派路径曾漏掉这一步"
        assert published[0]["task_id"] == "tsk_test_001"
        assert published[0]["robot_id"] == "robot_A"
        assert published[0]["lng"] == pytest.approx(120.5)
        assert published[0]["lat"] == pytest.approx(26.1)
        assert published[0]["priority"] == 1

        assert len(pushed) == 1, "看板没收到推送"
        # ★ 四个字段一个都不能少（api.md §「服务端推送消息」）
        assert set(pushed[0]) == {"task_id", "event_id", "robot_id", "status"}

    def test_push_alone_when_no_robot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """没有绑定机器人时不下发指令，但**仍然要推送看板**并留痕。"""
        from app.mqtt import client as mqtt_mod
        from app.services import dispatch as dispatch_mod
        from app.ws import manager as ws_mod

        published: list[dict] = []
        pushed: list[dict] = []

        async def fake_publish(**kwargs: object) -> bool:
            published.append(kwargs)
            return True

        async def fake_push(data: dict) -> None:
            pushed.append(data)

        monkeypatch.setattr(mqtt_mod.mqtt_client, "publish_task", fake_publish)
        monkeypatch.setattr(ws_mod.ws_manager, "push_task_update", fake_push)

        asyncio.run(dispatch_mod.finalize_dispatch(make_task(robot_id=None)))

        assert published == [], "没有机器人就不该下发指令"
        assert len(pushed) == 1, "看板仍需知道这个任务存在"

    def test_unparseable_location_still_pushes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """坐标解析不出来：不下发，但推送照做 —— 且不能静默（有 ERROR 日志）。"""
        from app.mqtt import client as mqtt_mod
        from app.services import dispatch as dispatch_mod
        from app.ws import manager as ws_mod

        published: list[dict] = []
        pushed: list[dict] = []

        async def fake_publish(**kwargs: object) -> bool:
            published.append(kwargs)
            return True

        async def fake_push(data: dict) -> None:
            pushed.append(data)

        monkeypatch.setattr(mqtt_mod.mqtt_client, "publish_task", fake_publish)
        monkeypatch.setattr(ws_mod.ws_manager, "push_task_update", fake_push)

        asyncio.run(dispatch_mod.finalize_dispatch(make_task(target_location="???")))

        assert published == []
        assert len(pushed) == 1


# ======================================================================
# 三、★ 穷举守卫：每个派单入口都必须接上收尾
# ======================================================================
class TestEveryDispatchEntryFinalizes:
    """AST 穷举 `dispatch_for_event` 的全部调用点。

    这是**锁根因**的守卫：不逐个列举入口（那样新增入口就漏了），
    而是「凡是派了单的地方，都必须收尾」。
    """

    def test_all_dispatch_sites_finalize(self) -> None:
        offenders: list[str] = []
        sites: dict[str, str] = {}

        for path in iter_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for func_name, node in enclosing_functions(tree):
                calls = collect_calls(node)
                if "dispatch_for_event" not in calls:
                    continue
                rel = path.relative_to(BACKEND.parent).as_posix()
                sites[f"{rel}::{func_name}"] = func_name
                if "finalize_dispatch" not in calls:
                    offenders.append(f"{rel}::{func_name}")

        assert not offenders, (
            "下列派单入口没有调用 finalize_dispatch，"
            "机器人将收不到指令 / 看板收不到推送，且不报错：\n  "
            + "\n  ".join(offenders)
            + "\n\n派单成功 = 建任务 + 下发 MQTT + 推送看板，三步缺一不可。"
        )

        # ---------- 自证：扫描器必须真的扫到了东西 ----------
        assert len(sites) >= 4, (
            f"只扫到 {len(sites)} 个派单入口（预期 ≥ 4）：{sorted(sites)}\n"
            "扫描器可能失效了 —— 这不是'没问题'，是'没在检查'。"
        )
        found_names = set(sites.values())
        missing = ONCE_BROKEN_ENTRY_POINTS - found_names
        assert not missing, (
            f"扫描结果里找不到曾经漏接的入口 {sorted(missing)}；\n"
            f"实际扫到：{sorted(found_names)}\n"
            "说明扫描器已经失效（例如改了 AST 节点类型），必须修守卫而不是跳过。"
        )

    def test_finalize_dispatch_is_complete(self) -> None:
        """收尾函数自己必须完整：下发与推送都在里面。"""
        tree = ast.parse((APP_DIR / "services" / "dispatch.py").read_text(encoding="utf-8"))
        node = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "finalize_dispatch"
        )
        calls = set(collect_calls(node))
        missing = {"publish_task", "push_task_update"} - calls
        assert not missing, (
            f"finalize_dispatch 缺少收尾动作 {sorted(missing)}；"
            "补派路径的机器人会收不到指令 / 看板收不到推送。"
        )


# ======================================================================
# 四、★ ACK 超时处理必须真的被接上
# ======================================================================
class TestAckTimeoutIsWired:
    """`handle_ack_timeout` 曾经**零调用点**。

    它是文档里「五步筛选」的第五步（ACK 超时换车重派）。
    实现了却没接线 → 机器人掉线后任务永久停在 assigned，
    不报错、不打日志，因为**根本没有代码在跑**。
    """

    def _call_sites(self, method: str) -> list[str]:
        out: list[str] = []
        for path in iter_python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(
                        fn, "id", None
                    )
                    if name == method:
                        out.append(
                            f"{path.relative_to(BACKEND.parent).as_posix()}:{node.lineno}"
                        )
        return out

    def test_handle_ack_timeout_has_external_caller(self) -> None:
        sites = self._call_sites("handle_ack_timeout")
        assert sites, (
            "handle_ack_timeout 没有任何调用点 —— 五步筛选的第五步形同虚设：\n"
            "  机器人掉线 / 不回 ACK 时，任务会永久停在 assigned，\n"
            "  且不报错、不打日志（因为没有任何代码在检查它）。\n"
            "必须在定时任务里周期性地调用它。"
        )

    def test_reassign_task_has_external_caller(self) -> None:
        """回退成 pending 之后必须有人重新派出去，否则只是换个状态继续卡。"""
        assert self._call_sites("reassign_task"), (
            "reassign_task 没有调用点：ACK 超时回退后的任务会一直停在 pending，\n"
            "因为「已被派过单」的事件不会被重新派单。"
        )

    def test_scanner_self_proof(self) -> None:
        """自证：扫描器能扫到已知存在的调用点，否则上面两条'通过'没有意义。"""
        assert self._call_sites("dispatch_for_event"), (
            "扫描器连 dispatch_for_event 都扫不到，说明它已经失效。"
        )


# ======================================================================
# 五、换车重派必须重置计时起点
# ======================================================================
class TestReassignResetsAssignedAt:
    """`transition` 只在 `assigned_at` 为空时才赋值。

    重派时若不重置，新一次的 ACK 超时判据会沿用**上一次**的时间戳，
    于是任务下一轮立刻又被判超时 → 每 30 秒空转重派一次，永远收敛不了。
    """

    def test_reassign_resets_assigned_at(self) -> None:
        src = (APP_DIR / "services" / "dispatch.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        node = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "reassign_task"
        )

        reset = False
        for sub in ast.walk(node):
            target = None
            if isinstance(sub, ast.Assign) and sub.targets:
                target = sub.targets[0]
            elif isinstance(sub, ast.AugAssign):
                target = sub.target
            if isinstance(target, ast.Attribute) and target.attr == "assigned_at":
                reset = True

        assert reset, (
            "reassign_task 没有重置 task.assigned_at。\n"
            "transition() 只在 assigned_at 为空时赋值，沿用旧时间戳会让任务\n"
            "下一轮立刻又被判成 ACK 超时，陷入 30 秒一次的空转重派。"
        )


# ======================================================================
# 六、ACK 超时必须同时兼容 aware / naive 数据库时间
# ======================================================================
class TestAckTimeoutTimezoneSafety:
    """TIMESTAMPTZ 读回 aware datetime，不能与 naive ``datetime.now()`` 相减。

    真实故障：补派线程每轮在 ``handle_ack_timeout`` 抛 TypeError，
    然后在 rollback 后读取已过期 ORM 属性，再抛 MissingGreenlet。
    结果是任务既不会回退，也没有可用的告警日志。
    """

    @staticmethod
    def _timed_out(assigned_at: datetime) -> bool:
        engine = DispatchEngine(session=None)  # type: ignore[arg-type]
        task = make_task(
            event_id=None,
            assigned_at=assigned_at,
            status="assigned",
        )
        return asyncio.run(engine.handle_ack_timeout(task))

    def test_aware_timestamp_does_not_crash(self) -> None:
        assigned_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        assert self._timed_out(assigned_at) is True

    def test_naive_legacy_timestamp_is_normalized(self) -> None:
        assigned_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        assert self._timed_out(assigned_at) is True
