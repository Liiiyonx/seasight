"""平台侧 ACK 接收：冻结回执信封解析、身份校验与幂等判定（WP-14C）。

修复的断链（WP-14C 目标）：`robot/{robot_id}/cmd/ack` 的处理器此前只认
顶层 `task_id`，而冻结回执信封（docs/device-interface.md §5）**没有**顶层
`task_id`，于是所有冻结 ACK 都被静默丢弃，任务永远停在 assigned。

本模块是纯逻辑（无 DB / 无 MQTT / 无网络 / 不 import edge.device_sim）：

- 入站解析：冻结信封字段优先（``ack_id / command_id / device_id / seq /
  received_at / accepted / reason / mode``），同时兼容旧字段
  （``task_id / robot_id / accepted / ts``）。字段缺失或类型错误只记
  warning 并丢弃，绝不让 MQTT 主循环抛异常。
- ``task_id`` 解析顺序：优先顶层兼容字段；否则仅接受
  ``command_id = cmd_{task_id}`` 并反推；两者同时存在但不一致则整条拒绝。
- 判定优先级固定为 ``duplicate > late > out_of_order > new``。
- :class:`AckTracker` 为**进程内内存**实现：``publish_task`` 仅在 MQTT
  发布成功后登记 ``command_id / device_id / seq / expires_at``（供 late
  判定）。进程重启后登记表丢失 —— 未登记命令**仍可正常接收**（按
  new/out_of_order 处理），但**不得伪判 late**；任务状态推进另有数据库
  状态机兜底（assigned 守卫），因此内存丢失不会造成重复推进。这是有意的
  回落策略，本模块不做任何持久化伪装。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from loguru import logger


class AckResult:
    """ACK 判定结果（冻结取值，与 edge/device_sim.AckResult 语义一致）。"""

    NEW = "new"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    LATE = "late"

    ALL: tuple[str, ...] = (NEW, DUPLICATE, OUT_OF_ORDER, LATE)


#: task_id 反推前缀：标准契约仅接受 ``command_id = cmd_{task_id}``
#: （与 ``app.mqtt.client.command_id_for_task`` 的派生规则一致）。
COMMAND_ID_TASK_PREFIX = "cmd_"

#: 仿真 ACK 的 command_id 扩展：``cmd_{task_id}:sim_{run_id}``。
#: 扩展只在 ``mode=simulation`` 且顶层 task_id 与任务一致时启用，真实设备
#: 的稳定命令 ID 契约不受影响。
SIMULATION_COMMAND_MARKER = ":sim_"


# ----------------------------------------------------------------------
# 入站解析
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class AckEnvelope:
    """解析后的回执信封（冻结字段优先，旧字段已归一化）。"""

    command_id: str
    task_id: str
    device_id: str
    seq: int
    received_at: float          # epoch 秒
    accepted: bool
    reason: str
    mode: str
    ack_id: str = ""
    legacy: bool = False        # 是否至少使用了一个旧字段（task_id/robot_id/ts）


def _nonempty_str(value: Any) -> str | None:
    """非空字符串才有效；None / 非 str / 空白串一律返回 None。"""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _parse_seq(value: Any) -> int | None:
    """seq 必须为非负整数；类型错误（None/bool/负数/非整数值）返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        # 整数形态的浮点（如 JSON 解析出的 1.0）宽容放行，其余拒绝
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        return None
    return None


def _parse_accepted(value: Any) -> bool | None:
    """accepted 必须可解释为布尔；类型错误返回 None。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1"):
            return True
        if text in ("false", "0"):
            return False
    return None


def parse_received_at(value: Any) -> float | None:
    """把 received_at（旧字段为 ts）解析成 epoch 秒；解析失败返回 None。

    兼容三种形态：
    - epoch 秒：``int`` / ``float`` / 纯数字字符串；
    - ISO-8601 字符串（含 ``Z`` 后缀与带时区偏移），naive 按 UTC 解释；
    - ``datetime`` 对象。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # 纯数字 → epoch 秒（孪生时间模型为 float）
        try:
            return float(text)
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def task_id_from_command_id(command_id: str) -> str | None:
    """仅当 ``command_id = cmd_{task_id}`` 时反推 task_id，否则返回 None。"""
    if not command_id.startswith(COMMAND_ID_TASK_PREFIX):
        return None
    task_id = command_id[len(COMMAND_ID_TASK_PREFIX):]
    return task_id or None


def simulation_task_id_from_command_id(
    command_id: str,
    top_task_id: str | None,
    mode: str,
) -> str | None:
    """解析受控的仿真 command_id 扩展，返回已验证的 task_id。

    为避免把真实设备的未知命令格式误放行，这里要求：
    - ``mode == "simulation"``；
    - 顶层 ``task_id`` 必须存在；
    - ``command_id`` 必须精确形如 ``cmd_{task_id}:sim_{run_id}``；
    - ``run_id`` 非空，防止退化成标准命令或空扩展。
    """
    if mode != "simulation" or top_task_id is None:
        return None
    prefix = f"{COMMAND_ID_TASK_PREFIX}{top_task_id}{SIMULATION_COMMAND_MARKER}"
    if not command_id.startswith(prefix):
        return None
    run_id = command_id[len(prefix):]
    return top_task_id if run_id else None


def parse_ack_envelope(payload: Any) -> AckEnvelope | None:
    """解析回执信封；字段缺失/类型错误/身份字段自相矛盾时记 warning 并返回 None。

    解析规则（冻结接口 WP-14C 条款 1~4）：
    1. 冻结信封字段优先，旧字段兜底；
    2. ``task_id`` 优先取顶层兼容字段，否则仅接受 ``command_id=cmd_{task_id}``
       反推；两者同时存在但不一致 → 整条拒绝；
    3. 只做字段级解析，设备身份三重校验（payload / topic / task.robot_id）
       在 handler 层完成（需要查库）。
    """
    if not isinstance(payload, dict):
        logger.warning(f"[MQTT-ACK] 回执报文不是 JSON 对象：{type(payload).__name__}")
        return None

    # ---- 冻结信封字段优先，旧字段兜底 ----
    frozen_device_id = _nonempty_str(payload.get("device_id"))
    legacy_robot_id = _nonempty_str(payload.get("robot_id"))
    if frozen_device_id is None and legacy_robot_id is None:
        logger.warning(
            f"[MQTT-ACK] 缺少 device_id（旧字段 robot_id 也没有），丢弃：{_preview(payload)}"
        )
        return None
    if frozen_device_id is not None and legacy_robot_id is not None \
            and frozen_device_id != legacy_robot_id:
        logger.warning(
            f"[MQTT-ACK] device_id={frozen_device_id} 与 robot_id={legacy_robot_id} "
            f"不一致，冻结字段优先"
        )
    device_id = frozen_device_id or legacy_robot_id

    # seq：冻结信封必填（非负整数）；旧格式（docs/mqtt-topics.md §5.5）不带
    # seq 字段 —— 缺失字段整体缺省为 0 兼容；字段存在但类型错误则丢弃。
    if "seq" not in payload:
        seq = 0
    else:
        seq = _parse_seq(payload.get("seq"))
        if seq is None:
            logger.warning(
                f"[MQTT-ACK] seq 类型错误（需非负整数），丢弃：{_preview(payload)}"
            )
            return None

    received_at = parse_received_at(
        payload.get("received_at", payload.get("ts"))
    )
    if received_at is None:
        logger.warning(
            f"[MQTT-ACK] received_at（旧字段 ts）缺失或不可解析，丢弃：{_preview(payload)}"
        )
        return None

    accepted = _parse_accepted(payload.get("accepted"))
    if accepted is None:
        logger.warning(f"[MQTT-ACK] accepted 缺失或类型错误，丢弃：{_preview(payload)}")
        return None

    # ---- command_id / task_id：冻结信封优先，旧字段兜底 ----
    # 解析规则（冻结接口 WP-14C 条款 1~2）：
    #   · 冻结信封携带 command_id → 优先用它；
    #   · 顶层兼容字段 task_id 优先作为 task_id 真源；
    #   · 仅有 command_id 时，仅接受 command_id=cmd_{task_id} 反推；
    #   · 两者同时存在但不一致 → 整条拒绝；
    #   · 仅有旧字段 task_id（无 command_id）→ 派生 command_id=cmd_{task_id}
    #     作为去重键（与平台自己的 command_id_for_task 同构，保证新旧两种
    #     报文对同一任务共享同一幂等键）。
    command_id = _nonempty_str(payload.get("command_id"))
    top_task_id = _nonempty_str(payload.get("task_id"))
    mode = str(payload.get("mode") or "")

    if command_id is None and top_task_id is None:
        logger.warning(
            f"[MQTT-ACK] 缺少 command_id（旧字段 task_id 也没有），丢弃：{_preview(payload)}"
        )
        return None

    if mode == "simulation":
        # 仿真扩展是白名单契约：必须同时携带顶层 task_id，且 command_id
        # 必须精确匹配 cmd_{task_id}:sim_{run_id}。不能回落到标准命令格式，
        # 否则会绕过仿真 ACK 的身份约束。
        simulation_task_id = (
            simulation_task_id_from_command_id(command_id, top_task_id, mode)
            if command_id is not None
            else None
        )
        if simulation_task_id is None:
            logger.warning(
                f"[MQTT-ACK] simulation 模式的 command_id/task_id 不符合扩展契约，"
                f"整条拒绝：{_preview(payload)}"
            )
            return None
        task_id = simulation_task_id
    elif command_id is not None:
        derived_task_id = task_id_from_command_id(command_id)
        if top_task_id is not None:
            if derived_task_id is None or top_task_id != derived_task_id:
                logger.warning(
                    f"[MQTT-ACK] 顶层 task_id={top_task_id} 与 command_id={command_id} "
                    f"不一致，整条拒绝：{_preview(payload)}"
                )
                return None
            task_id = top_task_id
        else:
            if derived_task_id is None:
                logger.warning(
                    f"[MQTT-ACK] 无法解析 task_id（无顶层 task_id，command_id={command_id} "
                    f"也不是 cmd_{{task_id}} 形态），丢弃：{_preview(payload)}"
                )
                return None
            task_id = derived_task_id
    else:
        # 旧字段兜底：command_id = cmd_{task_id}（与 command_id_for_task 同构）
        command_id = f"{COMMAND_ID_TASK_PREFIX}{top_task_id}"
        task_id = top_task_id

    return AckEnvelope(
        command_id=command_id,
        task_id=task_id,
        device_id=device_id,
        seq=seq,
        received_at=received_at,
        accepted=accepted,
        reason=str(payload.get("reason") or ""),
        mode=mode,
        ack_id=str(payload.get("ack_id") or ""),
        legacy=top_task_id is not None or legacy_robot_id is not None
        or "ts" in payload,
    )


def _preview(payload: dict[str, Any], limit: int = 160) -> str:
    """截断报文预览，避免一条坏报文把日志撑爆。"""
    text = str(payload)
    return text if len(text) <= limit else text[:limit] + "…"


# ----------------------------------------------------------------------
# ACK 幂等跟踪（进程内内存实现）
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class AckRecord:
    """一条回执的首次规范记录（duplicate 时原样返回）。"""

    command_id: str
    task_id: str
    device_id: str
    seq: int
    received_at: float
    accepted: bool
    reason: str
    mode: str
    outcome: str
    ack_id: str = ""


class AckTracker:
    """平台侧 ACK 幂等跟踪（进程内，内存实现；与孪生语义一致的冻结契约）。

    **不 import edge.device_sim** —— 平台侧逻辑禁止反向依赖孪生包，
    这里是平台自己的实现（docs/device-interface.md §5 的平台侧判据）。

    判定优先级：``duplicate > late > out_of_order > new``。
    - ``duplicate``：同 ``command_id`` 再次到达。返回首次规范回执，不重复生效。
    - ``late``：``received_at > expires_at``（仅对**已登记**命令判定；
      未登记命令不得伪判 late）。仍记录并允许状态推进。
    - ``out_of_order``：新回执 ``seq`` 小于该设备已 ACK 水位。仍记录；
      ``accepted=true`` 时允许对仍处于 assigned 的任务推进。
    - ``new``：首次按序到达。

    登记与持久化边界：
    - ``register_command`` 只应由 ``client.publish_task`` 在**发布成功后**
      调用（登记 command_id/device_id/seq/expires_at）。
    - 本表随进程丢失；重启后未登记命令照常接收（回落策略，见模块 docstring）。
    """

    def __init__(self) -> None:
        self._acks: dict[str, AckRecord] = {}
        self._deadlines: dict[str, float] = {}
        self._registered: dict[str, tuple[str, int]] = {}
        self._max_acked_seq: dict[str, int] = {}
        self._lock = threading.Lock()
        self.outcomes: list[str] = []
        self.counts: dict[str, int] = {r: 0 for r in AckResult.ALL}

    # ------------------------------------------------------------------
    # 登记
    # ------------------------------------------------------------------
    def register_command(
        self,
        command_id: str,
        device_id: str,
        seq: int,
        expires_at: float,
    ) -> None:
        """登记已发布命令（供 late 判定与审计）。

        只在 MQTT 发布成功后调用（由 ``MqttClient.publish_task`` 负责）；
        登记失败 / 进程重启后的回落策略见模块 docstring。
        ``device_id`` / ``seq`` 随命令一并留档（与下发报文同源），
        便于与回执交叉核对；过期时间 ``expires_at`` 用于 late 判定。
        """
        with self._lock:
            self._deadlines[command_id] = float(expires_at)
            self._registered[command_id] = (str(device_id), int(seq))

    def has_deadline(self, command_id: str) -> bool:
        """该命令是否已登记过期时间（供测试与日志区分「未登记」）。"""
        with self._lock:
            return command_id in self._deadlines

    def deadline_for(self, command_id: str) -> float | None:
        """返回已登记的过期时间（未登记返回 None）。"""
        with self._lock:
            return self._deadlines.get(command_id)

    # ------------------------------------------------------------------
    # 判定与登记
    # ------------------------------------------------------------------
    def classify(self, ack: AckEnvelope) -> str:
        """纯判定（不存储）：duplicate > late > out_of_order > new。

        供 handler 先判定、落库成功后再 :meth:`record` —— 保证内存跟踪
        与数据库任务状态在同一次处理中一致（DB 是权威，跟踪表是去重/审计）。
        """
        with self._lock:
            if ack.command_id in self._acks:
                return AckResult.DUPLICATE
            deadline = self._deadlines.get(ack.command_id)
            if deadline is not None and ack.received_at > deadline:
                return AckResult.LATE
            if ack.seq < self._max_acked_seq.get(ack.device_id, ack.seq):
                return AckResult.OUT_OF_ORDER
            return AckResult.NEW

    def record(self, ack: AckEnvelope, outcome: str | None = None) -> str:
        """登记一条回执（幂等），返回判定结果。

        同 ``command_id`` 再次登记返回 ``duplicate`` 且不改写首次规范回执。
        ``outcome`` 可显式传入（handler 已用 :meth:`classify` 判过）；
        缺省则内部判定。
        """
        with self._lock:
            existing = self._acks.get(ack.command_id)
            if existing is not None:
                result = AckResult.DUPLICATE
            else:
                result = outcome if outcome is not None else self._classify_unlocked(ack)
                self._acks[ack.command_id] = AckRecord(
                    command_id=ack.command_id,
                    task_id=ack.task_id,
                    device_id=ack.device_id,
                    seq=ack.seq,
                    received_at=ack.received_at,
                    accepted=ack.accepted,
                    reason=ack.reason,
                    mode=ack.mode,
                    outcome=result,
                    ack_id=ack.ack_id,
                )
                if result == AckResult.NEW:
                    self._max_acked_seq[ack.device_id] = max(
                        self._max_acked_seq.get(ack.device_id, -1), ack.seq
                    )
            self.outcomes.append(result)
            self.counts[result] += 1
            return result

    def _classify_unlocked(self, ack: AckEnvelope) -> str:
        """锁内判定（调用方必须已持有 self._lock）。"""
        deadline = self._deadlines.get(ack.command_id)
        if deadline is not None and ack.received_at > deadline:
            return AckResult.LATE
        if ack.seq < self._max_acked_seq.get(ack.device_id, ack.seq):
            return AckResult.OUT_OF_ORDER
        return AckResult.NEW

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def is_acked(self, command_id: str) -> bool:
        with self._lock:
            return command_id in self._acks

    def ack_for(self, command_id: str) -> AckRecord | None:
        """返回首次规范回执（duplicate 时原样返回）。"""
        with self._lock:
            return self._acks.get(command_id)


__all__ = [
    "AckResult",
    "AckEnvelope",
    "AckRecord",
    "AckTracker",
    "COMMAND_ID_TASK_PREFIX",
    "parse_ack_envelope",
    "parse_received_at",
    "simulation_task_id_from_command_id",
    "task_id_from_command_id",
]
