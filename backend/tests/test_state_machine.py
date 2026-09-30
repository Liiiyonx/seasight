"""任务状态机单元测试。

★ 这组测试守的是「工单流转不能出脏数据」这条底线。
没有状态机时，"已完成"的任务可能被误改回"作业中"，finished_at 被覆盖，报表全乱。
"""

from __future__ import annotations

import pytest

from app.models.task import ReviewResult, TaskStatus


class TestStatusConstants:
    """状态常量本身的约束。"""

    def test_all_contains_six_states(self) -> None:
        assert len(TaskStatus.ALL) == 6

    def test_labels_cover_all_states(self) -> None:
        """每个状态都必须有中文标签 —— 前端直接展示，缺了会显示 undefined。"""
        for status in TaskStatus.ALL:
            assert status in TaskStatus.LABELS, f"状态 {status} 缺少中文标签"
            assert TaskStatus.LABELS[status], f"状态 {status} 的标签为空"

    def test_transitions_table_covers_all_states(self) -> None:
        """迁移表必须覆盖所有状态，否则 can_transition 会对新状态静默返回 False。"""
        for status in TaskStatus.ALL:
            assert status in TaskStatus.TRANSITIONS, f"状态 {status} 不在迁移表中"

    def test_active_excludes_terminal_states(self) -> None:
        """活跃状态不能包含终态 —— 否则防重复派单会误判。"""
        assert TaskStatus.DONE not in TaskStatus.ACTIVE
        assert TaskStatus.CANCELLED not in TaskStatus.ACTIVE

    def test_active_is_subset_of_all(self) -> None:
        for status in TaskStatus.ACTIVE:
            assert status in TaskStatus.ALL


class TestLegalTransitions:
    """合法流转路径。"""

    @pytest.mark.parametrize(
        "current,target",
        [
            (TaskStatus.PENDING, TaskStatus.ASSIGNED),
            (TaskStatus.ASSIGNED, TaskStatus.NAVIGATING),
            (TaskStatus.NAVIGATING, TaskStatus.COLLECTING),
            (TaskStatus.COLLECTING, TaskStatus.DONE),
        ],
    )
    def test_happy_path(self, current: str, target: str) -> None:
        """主干路径：pending → assigned → navigating → collecting → done"""
        assert TaskStatus.can_transition(current, target), f"{current} → {target} 应被允许"

    @pytest.mark.parametrize(
        "current",
        [
            TaskStatus.PENDING,
            TaskStatus.ASSIGNED,
            TaskStatus.NAVIGATING,
            TaskStatus.COLLECTING,
        ],
    )
    def test_any_active_state_can_cancel(self, current: str) -> None:
        """任意活跃态都可取消。"""
        assert TaskStatus.can_transition(current, TaskStatus.CANCELLED)

    def test_assigned_can_fallback_to_pending(self) -> None:
        """★ ACK 超时必须能回退到 pending 重新派单。

        这是整个派单链路唯一允许的「倒退」，专门为 ACK 超时设计。
        如果这条被删掉，机器人离线时工单会永久卡在 assigned。
        """
        assert TaskStatus.can_transition(TaskStatus.ASSIGNED, TaskStatus.PENDING)


class TestIllegalTransitions:
    """非法流转必须被拒绝。"""

    @pytest.mark.parametrize(
        "current,target",
        [
            (TaskStatus.PENDING, TaskStatus.DONE),
            (TaskStatus.PENDING, TaskStatus.NAVIGATING),
            (TaskStatus.PENDING, TaskStatus.COLLECTING),
            (TaskStatus.ASSIGNED, TaskStatus.DONE),
            (TaskStatus.ASSIGNED, TaskStatus.COLLECTING),
            (TaskStatus.NAVIGATING, TaskStatus.DONE),
            (TaskStatus.NAVIGATING, TaskStatus.ASSIGNED),
            (TaskStatus.DONE, TaskStatus.COLLECTING),
            (TaskStatus.CANCELLED, TaskStatus.PENDING),
        ],
    )
    def test_jump_rejected(self, current: str, target: str) -> None:
        """跳步与倒流都应被拒绝。"""
        assert not TaskStatus.can_transition(current, target), f"{current} → {target} 应被拒绝"

    def test_done_is_terminal(self) -> None:
        """终态不能迁移到任何状态。"""
        for target in TaskStatus.ALL:
            assert not TaskStatus.can_transition(TaskStatus.DONE, target)

    def test_cancelled_is_terminal(self) -> None:
        for target in TaskStatus.ALL:
            assert not TaskStatus.can_transition(TaskStatus.CANCELLED, target)

    def test_no_self_transition(self) -> None:
        """同一个状态原地迁移无意义，应被拒绝（避免前端重复提交产生噪声日志）。"""
        for status in TaskStatus.ALL:
            assert not TaskStatus.can_transition(status, status), f"{status} 不应自迁移"

    def test_unknown_status_rejected(self) -> None:
        """未知状态串应安全拒绝，而不是抛异常。"""
        assert not TaskStatus.can_transition("nonexistent", TaskStatus.DONE)
        assert not TaskStatus.can_transition(TaskStatus.PENDING, "nonexistent")
        assert not TaskStatus.can_transition("", "")


class TestReviewResult:
    def test_all_four_values(self) -> None:
        assert len(ReviewResult.ALL) == 4

    def test_not_found_exists(self) -> None:
        """★ not_found 是误报统计的唯一依据，不能删。

        有了它才能回答「这个点位的误报率是多少」，
        没有它误报永远是个说不清的数字。
        """
        assert ReviewResult.NOT_FOUND in ReviewResult.ALL


class TestInvariants:
    """跨状态的全局不变量。"""

    def test_every_transition_target_is_valid_state(self) -> None:
        """迁移表里出现的所有目标状态都必须是合法状态。"""
        for current, targets in TaskStatus.TRANSITIONS.items():
            for target in targets:
                assert target in TaskStatus.ALL, f"{current} → {target} 中 {target} 不是合法状态"

    def test_every_transition_source_is_valid_state(self) -> None:
        for current in TaskStatus.TRANSITIONS:
            assert current in TaskStatus.ALL

    def test_no_transition_from_terminal(self) -> None:
        """终态的出边必须为空。"""
        assert TaskStatus.TRANSITIONS[TaskStatus.DONE] == set()
        assert TaskStatus.TRANSITIONS[TaskStatus.CANCELLED] == set()
