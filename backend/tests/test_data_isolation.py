"""数据隔离测试。

operator 角色按 township_scope 隔离数据（只看到本辖区），admin/viewer 看全部。
这是「每个账号独立数据、互不干扰」的核心 —— operator(马鼻镇) 登录后只看到
马鼻镇的事件/工单/统计，不会与其他账号的数据互相影响。
"""

from __future__ import annotations

from pathlib import Path

from app.core.geo import nearest_township as _geo_near
from app.models.event import Event
from app.models.task import Task
from app.services.report import nearest_township as _report_near


class TestTownshipField:
    def test_event_has_township(self) -> None:
        assert hasattr(Event, "township"), "Event 模型缺 township 字段（辖区隔离失效）"

    def test_task_has_township(self) -> None:
        assert hasattr(Task, "township"), "Task 模型缺 township 字段（辖区隔离失效）"

    def test_nearest_township_single_source(self) -> None:
        """core.geo 与 services.report 必须是同一实现（re-export，防止双真源）。"""
        assert _geo_near is _report_near, (
            "nearest_township 存在两个实现 —— 乡镇归属逻辑被复制，会漂移。"
        )


class TestOperatorScopeFilter:
    """接口层必须真的按 operator 的 township_scope 过滤，而不是只看不筛。"""

    def test_events_list_filters_by_scope(self, project_root: Path) -> None:
        src = (project_root / "backend/app/api/v1/events.py").read_text(encoding="utf-8")
        assert 'user.role == "operator"' in src, "事件列表未按 operator 辖区过滤"
        assert "township_scope" in src, "事件列表未引用 township_scope"

    def test_tasks_list_filters_by_scope(self, project_root: Path) -> None:
        src = (project_root / "backend/app/api/v1/tasks.py").read_text(encoding="utf-8")
        assert 'user.role == "operator"' in src, "工单列表未按 operator 辖区过滤"
        assert "township_scope" in src, "工单列表未引用 township_scope"

    def test_stats_dashboard_filters_by_scope(self, project_root: Path) -> None:
        src = (project_root / "backend/app/api/v1/stats.py").read_text(encoding="utf-8")
        assert 'user.role == "operator"' in src, "大屏统计未按 operator 辖区过滤"
