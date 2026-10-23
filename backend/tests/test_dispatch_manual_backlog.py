"""人工建单产生的「待派单工单」必须能被补派出去。

★ 缺陷形状（2026-09-27 线上验收实测，报告 P1-3）
──────────────────────────────────────────────
复现路径：

    工单看板 → 人工建单（不指定机器人）→ 工单落 `pending`、`robot_id` 为空
    → 点「触发补派」→ 接口 HTTP 200、`{"dispatched": 0}`、
      提示「完成补派 0 个任务」→ 工单**永远**停在待派单。

不报错、不抛异常、不打日志 —— 因为**根本没有代码扫到它**。

根因（`backend/app/services/dispatch.py`）::

    events = await self.events.recent_for_dispatch(limit=limit)   # 只扫 t_event

`recent_for_dispatch` 查的是 `t_event WHERE status='new'`，
而人工建单的工单 `event_id IS NULL`（见 `POST /tasks`），
它**没有事件可扫** —— 于是无论点多少次补派都扫不到。

★ 本文件锁三件事
────────────────
1. 补派必须同时覆盖「事件」和「无机器人的待派单工单」两条来源；
2. 扫工单的查询必须同时含 `status='pending'` 与 `robot_id IS NULL`
   —— 少了后者会把 ACK 超时退回的工单重复指派、覆盖 `assigned_at`；
3. 「挑哪台机器人」的判据只有一份（事件锚点与坐标锚点共用），
   否则人工补派会与事件派单按不同阈值静默分叉。

★ 行为验证不连数据库
────────────────────
`_assign_unassigned_pending` 只依赖 `self.tasks` / `self.devices` / `transition`，
用桩件替换前两者即可跑真实代码路径（`Task` / `Device` 是普通 ORM 实例，
不 flush 就不需要会话）。
"""

from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.models.device import Device  # noqa: E402
from app.models.task import Task, TaskStatus  # noqa: E402
from app.services.dispatch import DispatchEngine  # noqa: E402

DISPATCH_PY = "backend/app/services/dispatch.py"
REPOSITORY_PY = "backend/app/repositories/__init__.py"

# 人工建单的工单坐标：连江黄岐半岛附近
MANUAL_LNG = 119.90312
MANUAL_LAT = 26.35127


# ======================================================================
# 工具
# ======================================================================
def _find_function(project_root: Path, filename: str, func_name: str):
    """按名字取函数节点（必须先 parse 整个文件，不能按字符串切分）。"""
    src = (project_root / filename).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return node, src
    raise AssertionError(
        f"{filename} 里找不到 {func_name} —— 函数被改名或删除，本测试已失效"
    )


def _parse_point_ok(value) -> bool:
    from app.services.dispatch import _parse_point

    return _parse_point(value) is not None


# ======================================================================
# 一、★ AST：补派必须扫「工单」，而不只是「事件」
# ======================================================================
class TestPendingScanCoversManualTasks:
    """锁根因：`assign_pending_tasks` 的扫描面必须包含工单表。"""

    def test_assign_pending_scans_both_sources(self, project_root: Path) -> None:
        """★ 核心断言：函数体里必须同时出现「事件扫描」与「工单扫描」。

        用 `unassigned` 做关键字而非写死方法名：
        直连 `list_unassigned_pending` 或经由 `_assign_unassigned_pending`
        这类辅助方法都算通过，重构不会被误伤；
        而旧实现两样都没有 —— 它只扫事件，所以必然失败。
        """
        fn, _ = _find_function(project_root, DISPATCH_PY, "assign_pending_tasks")
        body = ast.unparse(fn)

        assert "recent_for_dispatch" in body, (
            "assign_pending_tasks 不再扫描待派单事件 —— 补派的第一类来源没了"
        )
        assert "unassigned" in body.lower(), (
            "assign_pending_tasks 只扫事件、不扫工单。\n"
            "人工建单（POST /tasks 未指定机器人）产生的工单 event_id 为空，\n"
            "没有任何事件可扫，于是点「触发补派」恒返回 dispatched=0，\n"
            "工单永远停在待派单，且不报错不打日志。\n"
            f"函数体：\n{body}"
        )

    def test_scanner_self_proof(self, project_root: Path) -> None:
        """自证：扫描器确实能读到函数体，否则上面那条'通过'没有意义。"""
        fn, _ = _find_function(project_root, DISPATCH_PY, "assign_pending_tasks")
        body = ast.unparse(fn)
        assert "events" in body, "扫描器没读到 assign_pending_tasks 的函数体"

    def test_backlog_helper_exists(self, project_root: Path) -> None:
        """辅助方法必须真的存在（避免用一句注释骗过上面的断言）。"""
        _find_function(project_root, DISPATCH_PY, "_assign_unassigned_pending")


# ======================================================================
# 二、★ 查询条件：`status='pending'` 与 `robot_id IS NULL` 缺一不可
# ======================================================================
class TestUnassignedPendingQuery:
    """那条 SQL 的两个条件各有明确语义，少一个就会出错。"""

    def test_query_filters_status_and_null_robot(self, project_root: Path) -> None:
        fn, _ = _find_function(project_root, REPOSITORY_PY, "list_unassigned_pending")
        body = ast.unparse(fn)

        assert "TaskStatus.PENDING" in body, (
            "list_unassigned_pending 未按 status='pending' 过滤"
        )
        assert "is_(None)" in body, (
            "list_unassigned_pending 未判 `robot_id IS NULL`。\n"
            "只按 status='pending' 取的话，会把 ACK 超时被 handle_ack_timeout\n"
            "退回的工单也捞进来（它们 pending 但**有**原定机器人）：\n"
            "重复指派会覆盖 assigned_at，让 ACK 超时判定失去计时起点。\n"
            "那类工单只能走 reassign_task（换车重派）。\n"
            f"函数体：\n{body}"
        )

    def test_query_orders_by_urgency(self, project_root: Path) -> None:
        """补派顺序应先紧急后等待时长，与事件补派「先到先处理」语义一致。"""
        fn, _ = _find_function(project_root, REPOSITORY_PY, "list_unassigned_pending")
        body = ast.unparse(fn)
        assert "priority" in body and "order_by" in body, (
            "list_unassigned_pending 没有按优先级排序 —— 补派顺序将变得不确定"
        )


# ======================================================================
# 三、★ 「挑哪台机器人」的判据只能有一份
# ======================================================================
class TestRobotSelectionSingleRule:
    """事件锚点与坐标锚点必须共用同一套筛选/加权规则。"""

    def test_thresholds_appear_once(self, project_root: Path) -> None:
        """两条入参路径若各写一份阈值，改策略时必然静默分叉。"""
        src = (project_root / DISPATCH_PY).read_text(encoding="utf-8")
        assert src.count("dispatch_min_battery") == 1, (
            "电量阈值在 dispatch.py 里出现了多次 —— 选机器人规则被复制了，"
            "事件派单与人工补派可能按不同阈值工作"
        )
        assert src.count("dispatch_max_bin_usage") == 1, (
            "仓容阈值在 dispatch.py 里出现了多次 —— 同上"
        )

    def test_event_anchor_delegates(self, project_root: Path) -> None:
        """事件锚点那条路必须**委托**给统一实现，而不是自己再挑一遍。"""
        fn, _ = _find_function(project_root, DISPATCH_PY, "_select_robot")
        body = ast.unparse(fn)
        assert "_select_robot_at_point" in body, (
            "_select_robot 没有委托给统一实现 —— 选机器人的规则又出现了第二份"
        )

    def test_coordinate_anchor_exists(self, project_root: Path) -> None:
        """人工补派必须有「按坐标选机器人」的入口（没有 event 可反查）。"""
        fn, _ = _find_function(project_root, DISPATCH_PY, "_select_robot_at_point")
        body = ast.unparse(fn)
        assert "find_nearest_available_robots_at" in body, (
            "_select_robot_at_point 未使用按坐标的 KNN 入口"
        )


# ======================================================================
# 四、★ 行为验证：跑真实的 _assign_unassigned_pending
# ======================================================================
class _FakeTaskRepo:
    def __init__(self, tasks: list[Task]) -> None:
        self._tasks = tasks
        self.limit_seen: int | None = None

    async def list_unassigned_pending(self, limit: int = 50) -> list[Task]:
        self.limit_seen = limit
        return self._tasks[:limit]


class _FakeDeviceRepo:
    """桩件：按坐标返回候选机器人，并记录每轮调用。

    ★ 若有人把人工补派改回按 `event_id` 反查坐标，
      这里会直接抛错 —— 那类工单根本没有事件。
    """

    def __init__(self, robots: list[Device]) -> None:
        self.robots = robots
        self.calls: list[dict] = []

    async def find_nearest_available_robots_at(
        self,
        lng: float,
        lat: float,
        *,
        max_distance_m: int = 5000,
        limit: int = 5,
        exclude_robot_ids: set[str] | None = None,
    ) -> list[tuple[Device, float]]:
        self.calls.append(
            {"lng": lng, "lat": lat, "exclude": set(exclude_robot_ids or ())}
        )
        excluded = set(exclude_robot_ids or ())
        return [(r, 120.0) for r in self.robots if r.device_id not in excluded][:limit]

    async def find_nearest_available_robots(self, *args, **kwargs):   # pragma: no cover
        raise AssertionError(
            "人工建单的工单没有 event 可反查，不能走按 event_id 的 KNN 入口"
        )


def _manual_task(task_id: str, lng: float = MANUAL_LNG, lat: float = MANUAL_LAT) -> Task:
    """复刻 `POST /tasks` 不指定机器人时的落库形态。"""
    return Task(
        task_id=task_id,
        event_id=None,          # ★ 人工建单：没有关联事件
        robot_id=None,
        target_location=f"SRID=4326;POINT({lng} {lat})",
        status=TaskStatus.PENDING,
        priority=3,
    )


def _robot(device_id: str) -> Device:
    return Device(
        device_id=device_id,
        device_type="robot",
        name=device_id,
        status="online",
        meta={"battery": 90, "bins": {}},
    )


class TestAssignManualBacklogBehaviour:
    """直接调用真实的 `_assign_unassigned_pending`（不连数据库）。"""

    @staticmethod
    def _engine(tasks: list[Task], robots: list[Device]) -> DispatchEngine:
        engine = DispatchEngine(session=None)   # type: ignore[arg-type]
        engine.tasks = _FakeTaskRepo(tasks)     # type: ignore[assignment]
        engine.devices = _FakeDeviceRepo(robots)  # type: ignore[assignment]
        return engine

    def test_manual_task_gets_assigned(self) -> None:
        """★ 本缺陷的直接回归：人工建单的工单必须被派出去。"""
        task = _manual_task("tsk_manual_1")
        engine = self._engine([task], [_robot("R-1")])

        assigned = asyncio.run(engine._assign_unassigned_pending(limit=10))

        assert [t.task_id for t in assigned] == ["tsk_manual_1"], (
            "人工建单的待派单工单没有被补派 —— P1-3 回归"
        )
        assert task.status == TaskStatus.ASSIGNED
        assert task.robot_id == "R-1"

    def test_assigned_at_is_set_for_ack_timeout(self) -> None:
        """`assigned_at` 是 ACK 超时判定的计时起点，补派时必须写上。"""
        task = _manual_task("tsk_manual_2")
        engine = self._engine([task], [_robot("R-1")])

        asyncio.run(engine._assign_unassigned_pending(limit=10))

        assert task.assigned_at is not None, (
            "补派出去的工单没有 assigned_at —— handle_ack_timeout 永远判不出超时，"
            "机器人掉线后工单会永久停在 assigned"
        )

    def test_robot_meta_points_at_task(self) -> None:
        """与 `_create_and_assign` 口径一致：机器人的当前任务指针要更新。"""
        task = _manual_task("tsk_manual_3")
        robot = _robot("R-1")
        engine = self._engine([task], [robot])

        asyncio.run(engine._assign_unassigned_pending(limit=10))

        assert (robot.meta or {}).get("current_task_id") == "tsk_manual_3"

    def test_no_robot_keeps_task_pending(self) -> None:
        """附近没有机器人时保持待派单，不抛异常（等机器人上线再补）。"""
        task = _manual_task("tsk_manual_4")
        engine = self._engine([task], [])

        assigned = asyncio.run(engine._assign_unassigned_pending(limit=10))

        assert assigned == []
        assert task.status == TaskStatus.PENDING
        assert task.robot_id is None

    def test_same_robot_not_reused_within_one_round(self) -> None:
        """★ 一轮补派多张工单时，同一台机器人不能被重复指派。

        `status='online'` 的查询会反复返回同一台机器人；不排除已派出的，
        多张工单会全部压给一台（且互相覆盖 `current_task_id`）。
        """
        t1 = _manual_task("tsk_manual_5", lng=119.90, lat=26.35)
        t2 = _manual_task("tsk_manual_6", lng=119.91, lat=26.36)
        engine = self._engine([t1, t2], [_robot("R-1")])

        assigned = asyncio.run(engine._assign_unassigned_pending(limit=10))

        assert [t.task_id for t in assigned] == ["tsk_manual_5"], (
            "同一轮里第二张工单不该再派给已经接单的 R-1"
        )
        assert t2.status == TaskStatus.PENDING
        # 第二次查询必须带上排除集
        device_repo: _FakeDeviceRepo = engine.devices  # type: ignore[assignment]
        assert device_repo.calls[-1]["exclude"] == {"R-1"}

    def test_two_robots_assign_two_tasks(self) -> None:
        """机器够用时应全部派出去。"""
        t1 = _manual_task("tsk_manual_7", lng=119.90, lat=26.35)
        t2 = _manual_task("tsk_manual_8", lng=119.91, lat=26.36)
        engine = self._engine([t1, t2], [_robot("R-1"), _robot("R-2")])

        assigned = asyncio.run(engine._assign_unassigned_pending(limit=10))

        assert sorted(t.task_id for t in assigned) == ["tsk_manual_7", "tsk_manual_8"]
        assert {t.robot_id for t in assigned} == {"R-1", "R-2"}

    def test_unparsable_coordinate_is_skipped_not_crashing(self) -> None:
        """坐标解析不出来时跳过并留痕，不能让整轮补派崩掉。"""
        bad = Task(
            task_id="tsk_bad",
            event_id=None,
            robot_id=None,
            target_location=12345,          # 不是任何可解析的几何值
            status=TaskStatus.PENDING,
            priority=3,
        )
        good = _manual_task("tsk_good")
        engine = self._engine([bad, good], [_robot("R-1")])

        assigned = asyncio.run(engine._assign_unassigned_pending(limit=10))

        assert [t.task_id for t in assigned] == ["tsk_good"], (
            "一张坏工单不该阻断后面工单的补派"
        )
        assert bad.status == TaskStatus.PENDING

    def test_empty_backlog_returns_immediately(self) -> None:
        """没有待派单工单时直接返回，不再做任何查询。"""
        engine = self._engine([], [_robot("R-1")])

        assert asyncio.run(engine._assign_unassigned_pending(limit=10)) == []
        assert (engine.devices).calls == []   # type: ignore[attr-defined]


# ======================================================================
# 五、坐标解析自证（行为测试依赖它）
# ======================================================================
class TestCoordinateParsingSelfProof:
    """上面那些行为测试的前提：EWKT 字符串真的能解析出坐标。"""

    def test_ewkt_string_parses(self) -> None:
        assert _parse_point_ok(f"SRID=4326;POINT({MANUAL_LNG} {MANUAL_LAT})")

    def test_nonsense_does_not_parse(self) -> None:
        assert not _parse_point_ok(12345)
        assert not _parse_point_ok(None)
