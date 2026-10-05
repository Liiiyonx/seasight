"""历史工单的「运行日志」必须来自真实落库记录，且不能恒为空。

★ 缺陷形状（2026-09-27 线上验收 P2-4）
──────────────────────────────────────
复现路径：

    工单看板 → 任意**历史**工单 → 进入「执行仿真」页
    → 右侧「机器人报文」页签有 20 条真实轨迹点
    → 切到「运行日志」页签 → 恒为「暂无可展示的运行日志」。

根因（`backend/app/api/v1/simulations.py`）::

    "error": None,
    "logs": [],        # ← 写死

`_history_snapshot` 是**历史工单**（无实时仿真会话）的唯一数据源，
它把 `logs` 定成空数组，于是任何历史工单的日志页签都不可能非空 ——
不是数据缺失，是代码没接。

★ 本文件锁两件事
1. `logs` 必须由真实记录派生（t_task 时间戳列 / t_task_ack 审计行 /
   t_track 首末点），并在 `_history_snapshot` 里真的接上；
2. 派生过程必须能承受 naive 与 aware 时间戳混排 ——
   PG 读回 aware、SQLite/历史数据 naive，混进同一个排序键会直接抛
   `TypeError`，一条日志都发不出去。
"""

from __future__ import annotations

import ast
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.api.v1.simulations import (  # noqa: E402
    _history_logs,
    _history_snapshot,
)
from app.models.task import Task, TaskStatus  # noqa: E402
from app.models.task_ack import TaskAck  # noqa: E402

SIMULATIONS_PY = "backend/app/api/v1/simulations.py"

T0 = datetime(2026, 9, 19, 2, 0, 0, tzinfo=timezone.utc)


def _task(**overrides) -> Task:
    fields = dict(
        task_id="tsk_hist_1",
        event_id=None,
        robot_id="RBT-001",
        target_location="SRID=4326;POINT(119.6518 26.3858)",
        status=TaskStatus.DONE,
        priority=1,
        township="马鼻镇",
        created_at=T0,
        assigned_at=T0 + timedelta(minutes=5),
        ack_at=T0 + timedelta(minutes=6),
        started_at=T0 + timedelta(minutes=10),
        finished_at=T0 + timedelta(minutes=40),
        collected_weight=Decimal("12.500"),
    )
    fields.update(overrides)
    return Task(**fields)


def _track(lng: float, lat: float, ts: datetime, battery: int = 90) -> dict:
    return {
        "seq": 1,
        "lng": lng,
        "lat": lat,
        "ts": ts.isoformat(),
        "battery": battery,
        "bins": {"foam": 0.1, "plastic": 0.05, "mixed": 0.02},
        "speed": 0.8,
    }


def _find_hardcoded_empty_logs(src: str) -> list[str]:
    """扫出源码里形如 `{"logs": []}` 的**字典字面量**（注释/字符串豁免）。

    只看 AST 里的 Dict 节点，所以 docstring 中提及该形状不会被误判。
    """
    tree = ast.parse(src)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value == "logs"):
                continue
            if isinstance(value, (ast.List, ast.Tuple)) and not value.elts:
                offenders.append(f"行 {node.lineno}: {ast.unparse(node)}")
    return offenders


def _ack(command_id: str = "cmd_abc", when: datetime | None = None) -> TaskAck:
    return TaskAck(
        command_id=command_id,
        task_id="tsk_hist_1",
        device_id="RBT-001",
        seq=7,
        outcome="new",
        accepted=True,
        reason="",
        mode="sim",
        received_at=when or (T0 + timedelta(minutes=7)),
        received_wall_at=when or (T0 + timedelta(minutes=7)),
        raw_payload={"ok": True},
    )


# ======================================================================
# 一、★ 根因守卫：不能再写死空数组
# ======================================================================
class TestLogsAreNotHardcodedEmpty:
    def test_source_does_not_hardcode_empty_logs(self, project_root: Path) -> None:
        """★ 直接锁缺陷形状：字典字面量里不得再出现 `"logs": []`。

        ★ 必须用 AST 而不是字符串匹配：
            `_history_logs` 的 docstring 里就写着这个形状（说明历史缺陷），
            字符串匹配会把自己的注释当成违规 —— 一条永远修不好的红灯。
            AST 只看**真实的字典字面量节点**，注释与文档字符串自动豁免。
        """
        src = (project_root / SIMULATIONS_PY).read_text(encoding="utf-8")
        offenders = _find_hardcoded_empty_logs(src)

        assert not offenders, (
            "simulations.py 里又有写死的空 logs：\n  "
            + "\n  ".join(offenders)
            + "\n\n历史工单的唯一数据源就是 _history_snapshot，写死空数组会让\n"
            "「运行日志」页签**永远**为空 —— 哪怕轨迹页签里躺着一堆真实数据。\n"
            "必须由 _history_logs 从落库记录派生。"
        )

    def test_scanner_self_proof(self) -> None:
        """自证：扫描器认得出这个写法，否则上面那条「通过」没有意义。"""
        sample = 'def f():\n    return {"logs": [], "x": 1}\n'
        assert _find_hardcoded_empty_logs(sample), (
            "扫描器扫不出 `{\"logs\": []}` —— 上面那条断言是假绿"
        )
        # 反向自证：注释/文档字符串里出现同样的形状不算违规
        comment_only = 'def f():\n    """过去写死 "logs": []。"""\n    return {"logs": fn()}\n'
        assert not _find_hardcoded_empty_logs(comment_only), (
            "扫描器把注释/文档字符串里的形状误判成违规"
        )

    def test_history_snapshot_calls_log_builder(self, project_root: Path) -> None:
        src = (project_root / SIMULATIONS_PY).read_text(encoding="utf-8")
        tree = ast.parse(src)
        node = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "_history_snapshot"
        )
        body = ast.unparse(node)
        assert "_history_logs" in body, (
            "_history_snapshot 没有调用 _history_logs —— 日志又变成写死的了"
        )

    def test_history_snapshot_result_is_non_empty(self) -> None:
        """★ 端到端形状：真实历史工单跑出来必须有日志。"""
        trajectory = [
            _track(119.6518, 26.3858, T0 + timedelta(minutes=11)),
            _track(119.6520, 26.3860, T0 + timedelta(minutes=30), battery=85),
        ]
        snapshot = asyncio.run(
            _history_snapshot(_task(), trajectory, [_ack()])
        )
        assert snapshot is not None
        assert snapshot["logs"], (
            "历史工单的 logs 仍为空 —— 「运行日志」页签没有内容可展示"
        )


# ======================================================================
# 二、日志内容：每一条都能追回一列或一行
# ======================================================================
class TestHistoryLogContent:
    def _logs(self, task=None, trajectory=None, acks=None) -> list[dict]:
        return _history_logs(task or _task(), trajectory or [], acks or [])

    def test_lifecycle_entries_present(self) -> None:
        kinds = [item["kind"] for item in self._logs()]
        assert "system" in kinds
        assert "control" in kinds      # 派单
        assert "progress" in kinds     # 开始清理
        assert "done" in kinds         # 完成

    def test_finished_message_carries_weight(self) -> None:
        done = next(i for i in self._logs() if i["kind"] == "done")
        assert "12.500" in done["message"], (
            "完成日志应带上真实打捞重量（来自 t_task.collected_weight）"
        )
        assert done["payload"]["collected_weight"] == 12.5

    def test_ack_entry_exposes_command_id(self) -> None:
        """前端靠 payload.command_id 渲染那行等宽编号。"""
        ack = next(i for i in self._logs(acks=[_ack("cmd_xyz")]) if i["kind"] == "ack")
        assert ack["payload"]["command_id"] == "cmd_xyz"
        assert ack["payload"]["outcome"] == "new"

    def test_telemetry_first_and_last_point(self) -> None:
        trajectory = [
            _track(119.6518, 26.3858, T0 + timedelta(minutes=11), battery=92),
            _track(119.6520, 26.3860, T0 + timedelta(minutes=30), battery=80),
        ]
        telemetry = [i for i in self._logs(trajectory=trajectory) if i["kind"] == "telemetry"]
        assert len(telemetry) == 2, "遥测首/末点各应产生一条"
        assert "92" in telemetry[0]["message"]
        assert "2 个轨迹点" in telemetry[1]["message"]

    def test_no_pending_task_yields_single_system_entry(self) -> None:
        """未派单的工单也要有可读内容，不能又是空白。"""
        task = _task(
            assigned_at=None, ack_at=None, started_at=None,
            finished_at=None, collected_weight=None, status=TaskStatus.PENDING,
        )
        logs = self._logs(task=task)
        assert logs, "未派单的历史工单也不该是完全空白"
        assert logs[0]["kind"] == "system"


# ======================================================================
# 三、★ 排序与时间戳兼容（naive + aware 混排）
# ======================================================================
class TestHistoryLogOrdering:
    def test_entries_sorted_chronologically(self) -> None:
        """★ 前端 `[...logs].reverse()` 假定输入是时间正序 —— 必须成立。"""
        trajectory = [
            _track(119.6518, 26.3858, T0 + timedelta(minutes=11)),
            _track(119.6520, 26.3860, T0 + timedelta(minutes=30)),
        ]
        logs = _history_logs(_task(), trajectory, [_ack()])
        stamps = [datetime.fromisoformat(item["ts"]) for item in logs]
        assert stamps == sorted(stamps), "日志不是按时间正序排列的"
        assert [item["seq"] for item in logs] == list(range(1, len(logs) + 1))

    def test_naive_timestamps_do_not_break_sorting(self) -> None:
        """★ PG 读回 aware、SQLite/历史数据 naive；混排不能让整批日志崩掉。"""
        naive_task = _task(
            created_at=datetime(2026, 9, 19, 2, 0, 0),          # naive
            assigned_at=datetime(2026, 9, 19, 2, 5, 0),         # naive
            started_at=T0 + timedelta(minutes=10),              # aware
            finished_at=T0 + timedelta(minutes=40),             # aware
        )
        logs = _history_logs(naive_task, [], [])
        assert logs, "naive/aware 混排时一条日志都没出来"
        stamps = [datetime.fromisoformat(item["ts"]) for item in logs]
        assert stamps == sorted(stamps)

    def test_duplicate_and_late_outcomes_are_labelled(self) -> None:
        dup = _ack("cmd_dup")
        dup.outcome = "duplicate"
        late = _ack("cmd_late", when=T0 + timedelta(minutes=50))
        late.outcome = "late"
        logs = [
            i for i in _history_logs(_task(), [], [dup, late]) if i["kind"] == "ack"
        ]
        assert "重复" in logs[0]["message"]
        assert "迟到" in logs[1]["message"]
