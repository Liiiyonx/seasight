"""CSV 下载回归测试：响应头可编码，中文文件名不触发 500。"""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import quote

from app.api.v1.reports import export_report
from app.api.v1.tasks import export_tasks, router as tasks_router
from app.core.downloads import attachment_header


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _statement):
        return _Rows(self._rows)


def _csv_rows(response) -> list[list[str]]:
    text = response.body.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def test_attachment_header_is_latin1_safe_and_preserves_utf8_name() -> None:
    filename = "治理日报_2026-09-19.csv"
    header = attachment_header(
        filename,
        ascii_fallback="seasight_report_2026-09-19.csv",
    )

    assert header.encode("latin-1")
    assert 'filename="seasight_report_2026-09-19.csv"' in header
    assert f"filename*=UTF-8''{quote(filename, safe='')}" in header


def test_report_export_has_safe_header_and_empty_null_area() -> None:
    row = SimpleNamespace(
        stat_date=date(2026, 9, 19),
        township="马鼻镇",
        main_class="foam",
        event_count=2,
        task_count=1,
        done_count=1,
        collected_kg=Decimal("3.20"),
        coverage_area=None,
        coverage_availability="not_available",
    )

    response = asyncio.run(
        export_report(days=7, township=None, session=_Session([row]))
    )

    export_date = date.today().isoformat()
    disposition = response.headers["content-disposition"]
    assert disposition.encode("latin-1")
    assert f'filename="seasight_report_{export_date}.csv"' in disposition
    rows = _csv_rows(response)
    assert rows[1][7] == ""
    assert rows[1][7] not in {"0", "0.00"}
    assert rows[1][8] == "not_available"


def test_task_export_static_route_precedes_dynamic_detail_route() -> None:
    paths = [route.path for route in tasks_router.routes]
    assert paths.index("/export") < paths.index("/{task_id}"), (
        "GET /tasks/export 会被 /tasks/{task_id} 提前吞掉，静态导出路由必须排在动态详情前"
    )


def test_task_export_has_safe_header() -> None:
    task = SimpleNamespace(
        task_id="tsk_20260919_abc123",
        event_id="evt_20260919_001",
        robot_id="robot_01",
        status="done",
        priority=2,
        collected_weight=Decimal("1.25"),
        review_result="confirmed",
        created_at=None,
        finished_at=None,
    )

    response = asyncio.run(
        export_tasks(status=None, robot_id=None, session=_Session([(task, 119.9, 26.3)]))
    )

    disposition = response.headers["content-disposition"]
    assert disposition.encode("latin-1")
    assert "filename*=UTF-8''" in disposition
    assert response.body.startswith(b"\xef\xbb\xbf")
