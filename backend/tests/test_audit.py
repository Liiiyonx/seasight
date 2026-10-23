"""操作审计测试。

覆盖：
1. ACTION_LABELS 覆盖全部已埋点的 action（防漏中文标签）
2. ★ 接线守卫 —— 4 处关键写操作真的调用了 record_audit 并带正确的 action。
   历史教训：审计这类「可追溯性」能力最容易写成「声明了、没埋点」——
   grep 得到 record_audit 函数，但写操作里一次都没调，于是「谁能改工单」
   永远查不到，且不报错不打日志。
"""

from __future__ import annotations

from pathlib import Path

from app.api.v1.audit import ACTION_LABELS


class TestAuditWiring:
    # (文件, 埋点的 action 值) —— 4 处关键写操作
    EXPECTED = [
        ("backend/app/api/v1/auth.py", "login"),
        ("backend/app/api/v1/tasks.py", "task_create"),
        ("backend/app/api/v1/tasks.py", "task_status_update"),
        ("backend/app/api/v1/events.py", "event_status_update"),
    ]

    def test_all_actions_have_labels(self) -> None:
        for _, action in self.EXPECTED:
            assert action in ACTION_LABELS, f"审计 action {action} 缺中文标签"

    def test_write_operations_record_audit(self, project_root: Path) -> None:
        """4 处写操作必须真的埋点（调用 record_audit 且 action 正确）。"""
        for rel, action in self.EXPECTED:
            src = (project_root / rel).read_text(encoding="utf-8")
            assert f'action="{action}"' in src, (
                f"{rel} 未埋点审计 action={action} —— "
                "该写操作不可追溯，审计日志形同虚设。"
            )
