"""记忆存储：分层业务记忆（手册 5.7），首版内存实现。

★ 结构化解密预算（手册 3.7）：只保存**决策摘要、工具输入摘要、输出摘要
与业务理由**，绝不保存模型私有思维链。`InMemoryMemoryStore.save` 会
强制检查内容中的禁止键（chain_of_thought / private_reasoning /
full_input / full_output / raw_reasoning），命中即拒绝写入。

记忆写入区分「事实 / 推断 / 人工确认」：`confidence` 承担置信度语义
（事实=1.0，推断<1.0），`source_type` 标明来源，禁止把推断写成事实。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from app.services.agents.errors import AgentError, TaskConflictError


def _time_key(value: datetime | None) -> datetime:
    """统一记忆排序时间：None 视为最早，naive 按 UTC，aware 转 UTC。"""
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

# 解密预算：禁止存入记忆的内容键（任何层级出现即拒绝）
FORBIDDEN_MEMORY_KEYS: tuple[str, ...] = (
    "chain_of_thought",
    "private_reasoning",
    "full_input",
    "full_output",
    "raw_reasoning",
)

# 允许的记忆类型（手册 5.7）
MEMORY_TYPES: tuple[str, ...] = ("working", "episodic", "semantic", "policy", "eval")


def validate_memory_content(content: Any) -> list[str]:
    """扫描记忆内容中的禁止键（递归），返回违规键列表。"""
    if isinstance(content, dict):
        found: list[str] = []
        for key, value in content.items():
            if key in FORBIDDEN_MEMORY_KEYS:
                found.append(key)
            found.extend(validate_memory_content(value))
        return found
    if isinstance(content, (list, tuple)):
        found = []
        for item in content:
            found.extend(validate_memory_content(item))
        return found
    return []


@dataclass
class MemoryEntry:
    """一条记忆（字段对齐 t_agent_memory）。

    content 只允许是决策摘要 / 工具输入输出摘要 / 业务理由。
    """

    memory_id: str
    memory_type: str  # MEMORY_TYPES 之一
    scope_type: str  # run / robot / township / ...
    scope_id: str
    content: Any
    confidence: float = 1.0
    source_type: str | None = None  # tool_call / policy / verification / replan / manual
    source_id: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    created_at: datetime | None = None


class MemoryStore(Protocol):
    """记忆存储协议（可注入，见手册 3.6）。

    协议方法体做参数校验后抛 NotImplementedError：既保证形参被真实
    使用（静态守卫），又保持纯协议语义 —— 具体行为由注入的实现提供。
    """

    def save(self, entry: MemoryEntry) -> MemoryEntry:
        if not isinstance(entry, MemoryEntry):
            raise TypeError(f"entry 必须是 MemoryEntry：{type(entry).__name__}")
        raise NotImplementedError("MemoryStore.save 未实现")

    def get(self, memory_id: str) -> MemoryEntry | None:
        if not isinstance(memory_id, str):
            raise TypeError(f"memory_id 必须是 str：{type(memory_id).__name__}")
        raise NotImplementedError("MemoryStore.get 未实现")

    def find(
        self,
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
        memory_type: str | None = None,
    ) -> list[MemoryEntry]:
        for name, value in (
            ("scope_type", scope_type),
            ("scope_id", scope_id),
            ("memory_type", memory_type),
        ):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} 必须是 str 或 None：{type(value).__name__}")
        raise NotImplementedError("MemoryStore.find 未实现")


class InMemoryMemoryStore:
    """内存记忆仓储（mem_ 前缀由可注入 ID 工厂生成，测试可确定复现）。"""

    def __init__(self, *, id_factory: Any = None, clock: Any = None) -> None:
        self._entries: dict[str, MemoryEntry] = {}
        self._id_factory = id_factory
        self._clock = clock

    def save(self, entry: MemoryEntry) -> MemoryEntry:
        violations = validate_memory_content(entry.content)
        if violations:
            raise AgentError(
                f"记忆内容包含禁止字段 {violations}：解密预算只允许保存决策摘要、"
                "工具输入摘要、输出摘要与业务理由",
                code="internal_error",
            )
        if entry.memory_type not in MEMORY_TYPES:
            raise AgentError(
                f"非法记忆类型 {entry.memory_type}，允许 {list(MEMORY_TYPES)}",
                code="internal_error",
            )
        if not entry.memory_id:
            if self._id_factory is not None:
                entry.memory_id = self._id_factory.new("mem_")
            else:
                import uuid

                entry.memory_id = f"mem_{uuid.uuid4().hex[:12]}"
        if entry.created_at is None:
            entry.created_at = (
                self._clock.now()
                if self._clock is not None
                else datetime.now(timezone.utc)
            )
        if entry.memory_id in self._entries:
            raise TaskConflictError(f"memory_id 重复：{entry.memory_id}")
        self._entries[entry.memory_id] = entry
        return entry

    def get(self, memory_id: str) -> MemoryEntry | None:
        return self._entries.get(memory_id)

    def find(
        self,
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
        memory_type: str | None = None,
    ) -> list[MemoryEntry]:
        result = list(self._entries.values())
        if scope_type is not None:
            result = [e for e in result if e.scope_type == scope_type]
        if scope_id is not None:
            result = [e for e in result if e.scope_id == scope_id]
        if memory_type is not None:
            result = [e for e in result if e.memory_type == memory_type]
        result.sort(key=lambda e: _time_key(e.created_at))
        return result

    def __len__(self) -> int:
        return len(self._entries)
