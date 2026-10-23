"""跨 run 经验（lessons）—— 让确定性规则模式也具备「可进化」闭环。

## 这个模块解决什么问题

规则模式的智能体有一个天然短板：它每次运行都从零开始，上一单踩过的坑
下一单还会再踩。头部项目（Mem0 / Letta）用向量记忆解决这件事，代价是
要引入 embedding 模型、向量库和一整套不确定的召回链路 —— 本项目的纪律
是不让核心能力依赖模型，所以这里走的是一条**确定性经验回路**：

    复盘（run 到终态）
      → 用可复现的规则从真实终态里提炼经验（lesson）
      → 落到 MemoryStore（memory_type=episodic，scope=事件类别）
      → 下一次 run 规划时按「条件」检索命中
      → 命中结果写进 plan 步骤的决策摘要，并按次递增命中计数
      → 轨迹里因此能看到「本次引用了哪条经验」
      → run 终态时结算「引用之后的真实结果」（settle）
      → 置信度 = 贝叶斯平滑后验：成功升、失败降（calibration.py）

最后一环是置信度校准闭环（参考决策模型/System One 路线的核心主张：
声明的置信度必须对齐实际成功率）。「被引用」只进审计计数，不进置信度；
置信度的证据只有两种 —— 复盘再次确认，与引用后 run 的真实结果。
`calibration_report()` 把全部「引用时置信度 → 结果」观测汇总成 ECE，
样本不足时显式返回 None，不硬给数字。

全程没有模型、没有向量、没有随机：同样的运行历史 + 同样的输入，
永远得到同样的经验引用，可离线复现。

## 为什么复用 MemoryStore 而不是新开一张表

手册 3.7 的 `t_agent_memory` 已经有
`memory_type / scope_type / scope_id / content / confidence / source_type`
五个字段，恰好够表达「**哪一类事件**（scope）在**什么条件下**该怎么做
（content）、有**多可信**（confidence）」。复用它 = 不改冻结契约。

命名空间约定（不新增列，靠取值区分）：
    memory_type = "episodic"        —— 事件性经验（区别于 run 级 working 记忆）
    scope_type  = "main_class"      —— 经验按事件类别聚合
    scope_id    = <事件类别>        —— 如 foam / plastic
    content     = {"kind":..., "condition":..., "guidance":...}
    source_id   = 提炼出该经验的 run_id

## 诚实性约束（这个项目的老规矩）

1. 经验只由**确定性规则**从 run 的真实终态提炼 —— 失败就是失败，
   成功就是成功，不做「推测性归因」。
2. 置信度是**贝叶斯平滑后验**（calibration.bayesian_confidence）：
   先验由经验种类的证据强度决定，观测是「复盘再确认 + 引用后真实
   结果」。成功上调、失败下调，封顶 0.95（规则模式的经验是启发式，
   不是定理，永远不到 1.0）。「被引用」本身不是正确性证据，只进
   审计计数 —— 这是与旧「固定步长递增」公式的本质区别。
3. 存储是**进程内**的（MemoryStore 目前只有内存实现），只在这一个后端
   进程活着期间累积。重启后经验清零 —— 这一点写在这里，也写在
   `agents.py` 的装配注释里，不假装它持久化了。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.services.agents.calibration import (
    bayesian_confidence,
    expected_calibration_error,
    reliability_buckets,
)
from app.services.agents.memory import MemoryEntry, MemoryStore

# 记忆命名空间（见模块 docstring）
LESSON_MEMORY_TYPE = "episodic"
LESSON_SCOPE_TYPE = "main_class"
LESSON_SOURCE_TYPE = "run_review"

# 经验种类：取值即语义，不允许自由字符串，便于穷举与对账。
LESSON_KINDS: tuple[str, ...] = (
    "merge_preferred",        # 近邻已有同类活跃工单 → 合并优于新建
    "replan_recovered",       # 首次工具失败后重规划可恢复 → 允许一次重试
    "high_risk_priority",     # 政策重点类别 → 优先派单、优先顺路机器人
    "no_robot_standby",       # 候选池为空/都不合格 → 先补池，别立刻重试
    "evidence_insufficient",  # 证据不足被研判拦下 → 转人工，别自动派单
)

# 每种经验的初始置信度（由该种类结论的证据强度决定，不是统一常数）
_KIND_BASE_CONFIDENCE: dict[str, float] = {
    # 合并是幂等语义保证的，证据最强
    "merge_preferred": 0.85,
    # 「重规划可恢复」来自一次真实恢复，样本少，起点低
    "replan_recovered": 0.60,
    # 类别优先级来自政策本身，不是统计结论
    "high_risk_priority": 0.80,
    # 资源不足是硬事实
    "no_robot_standby": 0.75,
    # 研判门禁是确定性规则
    "evidence_insufficient": 0.90,
}

# 置信度的先验等效样本数（α）：先验抵得过 α 次观测 —— 前几次失败不会
# 把一条政策级经验一棍子打死，但长期结果始终压过先验。
_CONFIDENCE_PRIOR_STRENGTH = 4.0
# 封顶（永不到 1.0）：规则模式的经验是启发式，不是定理
_CONFIDENCE_CEILING = 0.95
# 校准报告给出 ECE 的最小观测数：样本更少时返回 None，不给虚数字
_MIN_OBSERVATIONS_FOR_ECE = 8

# 每种经验的「适用条件」谓词键（对应 lessons_context 里的字段）。
# 条件不是装饰：match() 会真的按它过滤，不满足就不引用。
_KIND_PREDICATES: dict[str, str] = {
    "merge_preferred": "merge_candidate",
    "no_robot_standby": "no_candidate",
    "replan_recovered": "",             # 无条件，凡同类事件都适用
    "high_risk_priority": "high_priority",
    "evidence_insufficient": "evidence_insufficient",
}

_KIND_TEXT: dict[str, tuple[str, str]] = {
    "merge_preferred": (
        "近邻已有同类活跃工单（合并窗口内）",
        "优先并入既有工单而非新建，减少重复出勤",
    ),
    "replan_recovered": (
        "同类事件出现过首次工具失败后重规划恢复",
        "首次调用失败时允许一次重规划，不要直接判死",
    ),
    "high_risk_priority": (
        "该类别属政策重点治理类别",
        "优先派单，并优先选择同类别顺路机器人",
    ),
    "no_robot_standby": (
        "同类事件曾因候选机器人全部不合格而无法派单",
        "先补池/等待充电与清仓，不要立刻重复触发自动派单",
    ),
    "evidence_insufficient": (
        "同类事件曾因证据不足被研判拦下",
        "缺证据图或置信度不达标时转人工复核，不做自动派单",
    ),
}

# 命中次数上限（防止无限增长；也避免单条经验被"刷"到高置信）
_MAX_HIT_COUNT = 99


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


@dataclass
class Lesson:
    """一条经验（对外暴露的只读视图）。"""

    lesson_id: str
    scope_id: str            # 事件类别
    kind: str                # LESSON_KINDS 之一
    condition: str           # 人话条件
    guidance: str            # 结论 / 动作建议
    confidence: float
    hit_count: int = 0
    confirm_count: int = 0
    # 引用后已结算结果的 run 数（use_total）与其中成功数（use_success）。
    # ★ 这两个字段是校准证据：由 settle() 在 run 终态写入，不封顶
    #   （封顶 = 校准数据失真）；hit/confirm 是审计计数，封顶 99。
    use_total: int = 0
    use_success: int = 0
    source_run_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def describe(self) -> str:
        """给轨迹摘要用的一句话（控制在一条截图能读完的长度）。"""
        return f"{self.scope_id}/{self.kind}：{self.guidance}（置信度 {self.confidence:.2f}）"


@dataclass
class _LessonState:
    """内存中的当前态；每次变更都往 MemoryStore 追加一条审计记录。"""

    lesson: Lesson
    memory_id: str  # 最近一条记忆记录的 id（便于溯源）


class LessonStore:
    """经验库：在 MemoryStore 之上做确定性检索与命中计数。

    `memory` 注入既有的记忆仓储（内核的同一个 `self.memory`），
    所以「run 级 working 记忆」和「跨 run 经验」共用一套存储与脱敏纪律。

    `enabled=False` 时 `match()` 恒返回空列表、`harvest()` 直接返回 ——
    评测基线场景构建自己的 RuntimeConfig，不会因为本模块产生行为差异。
    """

    def __init__(
        self,
        memory: MemoryStore,
        *,
        enabled: bool = True,
        id_factory: Any = None,
        clock: Any = None,
    ) -> None:
        self.memory = memory
        self.enabled = enabled
        self._id_factory = id_factory
        self._clock = clock
        # key = (scope_id, kind) —— 同类同种经验只有一条，靠命中次数累积
        self._lessons: dict[tuple[str, str], _LessonState] = {}
        # 校准状态（进程内）：
        #   _pending_uses：run_id → [(scope_id, kind, 引用时声明的置信度)]
        #     note_hit 时登记，run 终态由 settle() 结算（pop，天然幂等）；
        #   _observations：全部已结算的 (引用时置信度, 结果是否成功)
        #     —— calibration_report() 的 ECE 输入，「越用越聪明」的证据线。
        self._pending_uses: dict[str, list[tuple[str, str, float]]] = {}
        self._observations: list[tuple[float, bool]] = []

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def all_lessons(self) -> list[Lesson]:
        """全部经验（按置信度降序、其次按创建时间）。"""
        items = [state.lesson for state in self._lessons.values()]
        items.sort(
            key=lambda item: (
                -item.confidence,
                item.created_at or datetime.min.replace(tzinfo=timezone.utc),
            )
        )
        return items

    def match(self, *, main_class: str, context: dict[str, Any] | None = None) -> list[Lesson]:
        """按事件类别 + 上下文条件检索命中的经验。

        条件判定完全确定性：谓词键在 `context` 里为真才命中。
        `replan_recovered` 无谓词，凡同类事件都适用。
        """
        if not self.enabled or not main_class:
            return []
        ctx = context or {}
        hits: list[Lesson] = []
        for (scope_id, kind), state in self._lessons.items():
            if scope_id != main_class:
                continue
            predicate = _KIND_PREDICATES.get(kind, "")
            if predicate and not ctx.get(predicate):
                continue
            hits.append(state.lesson)
        hits.sort(key=lambda item: (-item.confidence, item.kind))
        return hits

    # ------------------------------------------------------------------
    # 复盘 / 提炼
    # ------------------------------------------------------------------

    def harvest(
        self,
        *,
        run_id: str,
        main_class: str,
        outcome: str,
        termination_reason: str | None,
        bindings: dict[str, Any] | None,
        replan_count: int = 0,
    ) -> list[Lesson]:
        """从一个已到终态的 run 里提炼经验（确定性规则，不看模型）。

        返回本次新增/更新的经验列表，空列表表示这次复盘没有可提炼的结论
        —— 这是常态，不为了"有产出"而硬编一条经验出来。
        """
        if not self.enabled or not main_class:
            return []
        data = bindings or {}
        kinds: list[str] = []

        if outcome == "succeeded":
            if data.get("action") == "merged":
                kinds.append("merge_preferred")
            if int(replan_count or 0) > 0:
                kinds.append("replan_recovered")
            if data.get("risk_level") == "high":
                kinds.append("high_risk_priority")
        elif outcome == "failed":
            if termination_reason == "no_robot_available":
                kinds.append("no_robot_standby")
            if (
                termination_reason == "policy_denied"
                and data.get("recommended_action") == "manual_review"
            ):
                kinds.append("evidence_insufficient")

        return [
            self._upsert(scope_id=main_class, kind=kind, run_id=run_id)
            for kind in kinds
        ]

    def note_hit(self, lessons: list[Lesson], *, run_id: str) -> None:
        """记录"这批经验被这一次 run 引用了"。

        ★ 与 harvest 的再次确认分开计数（hit_count / confirm_count）：
          「被引用」和「被新证据再次确认」是两件事，混在一个计数里
          会让"引用 5 次"变成一句无法解释的数字。
        ★ 同时登记校准待结算项（引用时声明的置信度）：run 终态由
          settle() 把「引用之后的真实结果」补上。引用 ≠ 正确，引用后
          的结果才是置信度的证据。
        """
        if not self.enabled:
            return
        uses = self._pending_uses.setdefault(run_id, [])
        for lesson in lessons:
            state = self._lessons.get((lesson.scope_id, lesson.kind))
            if state is None:
                continue
            self._bump(state, run_id=run_id, as_hit=True)
            uses.append((state.lesson.scope_id, state.lesson.kind, state.lesson.confidence))

    def settle(self, *, run_id: str, outcome: str) -> int:
        """结算一个已到终态的 run「引用过的经验」的结果观测。

        ★ 校准闭环的收口：`note_hit` 只记录「被引用」，本方法在 run
          终态把「引用之后结果如何」补上 —— 这正是决策校准（System One
          路线）的核心主张：声明的置信度必须对齐实际成功率，ECE 越
          低越可信。
        ★ 只接受 succeeded / failed（与 harvest 同口径）：取消与超时是
          「人的决定」或「环境问题」，不构成经验的对错证据。
        ★ 幂等：待结算项按 run_id 一次性 pop，重复调用返回 0。
        ★ 「使用时声明的置信度」以引用时刻为准（note_hit 已登记），
          不是结算时刻 —— ECE 衡量的是「当时的声明」对不对。

        返回本次实际结算的观测条数。
        """
        if not self.enabled or outcome not in ("succeeded", "failed"):
            return 0
        settled = 0
        for scope_id, kind, confidence_at_use in self._pending_uses.pop(run_id, []):
            state = self._lessons.get((scope_id, kind))
            if state is None:
                # note_hit 时存在、结算前被移除：理论上不发生，防御性跳过
                continue
            lesson = state.lesson
            lesson.use_total += 1
            if outcome == "succeeded":
                lesson.use_success += 1
            self._observations.append((confidence_at_use, outcome == "succeeded"))
            self._recompute_confidence(lesson)
            lesson.updated_at = (
                self._clock.now() if self._clock is not None else datetime.now(timezone.utc)
            )
            state.memory_id = self._append_memory(lesson, run_id=run_id)
            settled += 1
        return settled

    def calibration_report(self, *, n_bins: int = 10) -> dict[str, Any]:
        """汇总当前全部「引用时置信度 → 结果」观测的校准报告。

        返回 `{observation_count, ece, buckets}`：
        - `ece`：期望校准误差（声明置信度 vs 实际成功率的加权偏差），
          观测数不足 `_MIN_OBSERVATIONS_FOR_ECE` 时为 None —— 样本太少
          时给数字是虚报，不是证据；
        - `buckets`：可靠性曲线分桶数据（range / count / avg_confidence /
          success_rate），可直接渲染成图。

        ★ 这是「可进化」的量化证据线：随观测累积，ECE 的走势就是
          「置信度声明越来越可信」的可回放记录。
        """
        count = len(self._observations)
        return {
            "observation_count": count,
            "ece": (
                round(expected_calibration_error(self._observations, n_bins=n_bins), 4)
                if count >= _MIN_OBSERVATIONS_FOR_ECE
                else None
            ),
            "buckets": reliability_buckets(self._observations, n_bins=n_bins),
        }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _upsert(self, *, scope_id: str, kind: str, run_id: str) -> Lesson:
        existing = self._lessons.get((scope_id, kind))
        if existing is not None:
            self._bump(existing, run_id=run_id, as_hit=False)
            return existing.lesson

        condition, guidance = _KIND_TEXT[kind]
        now = self._clock.now() if self._clock is not None else datetime.now(timezone.utc)
        lesson = Lesson(
            lesson_id=(
                self._id_factory.new("lsn_")
                if self._id_factory is not None
                else f"lsn_{scope_id}_{kind}"
            ),
            scope_id=scope_id,
            kind=kind,
            condition=condition,
            guidance=guidance,
            confidence=_KIND_BASE_CONFIDENCE[kind],
            hit_count=0,
            confirm_count=0,
            source_run_id=run_id,
            created_at=now,
            updated_at=now,
        )
        memory_id = self._append_memory(lesson, run_id=run_id)
        self._lessons[(scope_id, kind)] = _LessonState(lesson=lesson, memory_id=memory_id)
        return lesson

    def _bump(self, state: _LessonState, *, run_id: str, as_hit: bool) -> None:
        lesson = state.lesson
        if as_hit:
            lesson.hit_count = min(_MAX_HIT_COUNT, lesson.hit_count + 1)
        else:
            lesson.confirm_count = min(_MAX_HIT_COUNT, lesson.confirm_count + 1)
        self._recompute_confidence(lesson)
        lesson.updated_at = (
            self._clock.now() if self._clock is not None else datetime.now(timezone.utc)
        )
        lesson.source_run_id = run_id
        state.memory_id = self._append_memory(lesson, run_id=run_id)

    def _recompute_confidence(self, lesson: Lesson) -> None:
        """置信度 = 贝叶斯平滑后验（calibration.bayesian_confidence）。

        ★ 「被引用」（hit_count）不参与置信度：引用本身不是正确性
          证据，只进审计计数。证据只有两种 —— 复盘再次确认
          （confirm_count，等价一次成功观测）与引用后 run 的真实结果
          （use_success / use_total）。
        ★ 失败会让置信度下降 —— 与旧「基准 + 步长 × 计数」公式的本质
          区别：只会涨的置信度不是置信度，是计数器。
        """
        base = _KIND_BASE_CONFIDENCE[lesson.kind]
        successes = lesson.use_success + lesson.confirm_count
        trials = lesson.use_total + lesson.confirm_count
        lesson.confidence = min(
            _CONFIDENCE_CEILING,
            round(
                bayesian_confidence(
                    base,
                    successes,
                    trials,
                    prior_strength=_CONFIDENCE_PRIOR_STRENGTH,
                ),
                4,
            ),
        )

    def _append_memory(self, lesson: Lesson, *, run_id: str) -> str:
        """把经验当前态追加进 MemoryStore（只追加，不就地改写历史）。

        为什么是追加而不是更新：记忆表的语义是「留痕」。就地改写会让
        "这条经验是怎么变可信的" 无从追溯；追加之后，命中计数与置信度的
        演化本身就是一条可回放的审计线。
        """
        entry = MemoryEntry(
            memory_id="",
            memory_type=LESSON_MEMORY_TYPE,
            scope_type=LESSON_SCOPE_TYPE,
            scope_id=lesson.scope_id,
            content={
                "kind": lesson.kind,
                "condition": lesson.condition,
                "guidance": lesson.guidance,
                "hit_count": lesson.hit_count,
                "confirm_count": lesson.confirm_count,
                "use_total": lesson.use_total,
                "use_success": lesson.use_success,
            },
            confidence=lesson.confidence,
            source_type=LESSON_SOURCE_TYPE,
            source_id=run_id,
            created_at=lesson.updated_at,
        )
        saved = self.memory.save(entry)
        return saved.memory_id

    def __len__(self) -> int:
        return len(self._lessons)


def summarize_hits(lessons: list[Lesson]) -> str:
    """把命中经验拼成轨迹摘要里的「本次引用经验」片段。

    只列最强的一条 + 总条数：plan 步骤的决策摘要要能在一屏里读完，
    把 5 条经验全文贴进去等于没人看。
    """
    if not lessons:
        return ""
    top = lessons[0]
    if len(lessons) == 1:
        return f"本次引用经验：{top.describe()}"
    return f"本次引用经验 {len(lessons)} 条，首条：{top.describe()}"


__all__ = [
    "LESSON_KINDS",
    "LESSON_MEMORY_TYPE",
    "LESSON_SCOPE_TYPE",
    "LESSON_SOURCE_TYPE",
    "Lesson",
    "LessonStore",
    "summarize_hits",
]
