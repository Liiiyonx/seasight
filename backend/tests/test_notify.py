"""告警外发测试。

覆盖：
1. build_alert_text 纯函数 —— 告警正文包含关键字段
2. send_wecom_alert 未配置 webhook 时静默跳过（不抛异常）
3. ★ 接线守卫 —— handle_event 真的调用了 send_wecom_alert，
   否则告警外发就是「实现了 N 步、只接上 N-1 步」的死代码
"""

from __future__ import annotations

import asyncio
import ast
from pathlib import Path

from app.core.config import settings
from app.services.notify import build_alert_text, send_wecom_alert


class TestBuildAlertText:
    def test_contains_all_fields(self) -> None:
        text = build_alert_text("泡沫类", "CAM-01", 119.6521, 26.3864, "2026-09-18T08:00:00")
        assert "泡沫类" in text
        assert "CAM-01" in text
        assert "119.6521" in text
        assert "26.3864" in text
        assert "2026-09-18" in text


class TestSendWecomAlert:
    def test_skips_when_webhook_empty(self, monkeypatch) -> None:
        """webhook 未配置 → 静默跳过，不抛异常（「未启用」而非「失败」）。"""
        monkeypatch.setattr(settings, "wecom_webhook", "")
        assert asyncio.run(send_wecom_alert("test")) is False


class TestNotifyWiring:
    def test_handle_event_calls_send_wecom_alert(self, project_root: Path) -> None:
        """handle_event 必须真的调用 send_wecom_alert（否则告警外发未接线）。"""
        src = (project_root / "backend/app/mqtt/handlers.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_event":
                target = node
                break
        assert target is not None, "handlers.py 里找不到 handle_event"

        seg = ast.get_source_segment(src, target) or ""
        assert "send_wecom_alert" in seg, (
            "handle_event 未调用 send_wecom_alert —— 告警外发是死代码，"
            "「告警推到人」的能力实际上没接上。"
        )
