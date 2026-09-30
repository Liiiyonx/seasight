"""WP-14 设备孪生 —— 机器人作业单元数字孪生（DeviceTwin）。

独立于感知事件模拟器（`edge/simulator/`）的一套确定性设备模型：

- **命令闭环**：订阅命令（dispatch / pause / resume / return_home /
  emergency_stop / ack），按 `seq` 处理，对每条命令回 ACK。
- **ACK 幂等**：同一 `command_id` 重复到达只执行一次，回执重复投递由
  平台侧 :class:`~protocol.AckTracker` 去重（重复 / 乱序 / 超时）。
- **遥测闭环**：每个 tick 产出冻结最小字段遥测（电量、仓容、位置、
  时间戳、模式、任务号、故障码）。
- **断网排队 / 重连补传**：传输断开时出站报文进入本地 Outbox，
  重连后按原顺序补传。
- **紧急停止优先级**：`emergency_stop` 命令在处理批次中插队最先执行，
  执行后进入 `emergency_stop` 模式，其余待处理命令被拒绝（并回
  rejected ACK）；`resume` 恢复到停止前模式。
- **故障与恢复闭环**：故障码经 :meth:`inject_fault` / :meth:`clear_fault`
  注入与清除；天然故障（低电量、仓满）由状态机自动产生并自动恢复
  （自动返航 → 卸仓 / 充电）。

全部为 E1/E2：孪生行为与闭环在假时钟 + 内存 transport 下确定可复现，
不代表真实设备、真实边缘盒或现场验证。
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .protocol import (
    COMMAND_ACTIONS,
    FAULT_CODES,
    FAULT_NONE,
    FAULT_PRIORITY,
    Ack,
    Clock,
    DeviceCommand,
    DeviceMode,
    FakeClock,
    IdFactory,
    Position,
    SequentialIdFactory,
    Telemetry,
)
from .transport import DeviceTransport, MemoryTransport

logger = logging.getLogger("device_sim")

#: 遥测序号持久化的环境变量名（设置后默认写盘；构造参数 ``seq_store_path`` 优先）。
DEVICE_SIM_SEQ_FILE_ENV = "DEVICE_SIM_SEQ_FILE"


# ----------------------------------------------------------------------
# 遥测序号跨进程持久化（WP-14E）
# ----------------------------------------------------------------------
def load_seq_from_file(path: str | os.PathLike) -> tuple[int, str]:
    """读取上次持久化的遥测序号，返回 ``(seq, status)``。

    status 取值：
    - ``"ok"``：文件为合法 JSON（``{"seq": N}``，N ≥ 0）；
    - ``"missing"``：文件不存在（首次运行，正常从 0 开始）；
    - ``"corrupt"``：文件存在但损坏/不可解析 —— 尽力取**最后一个可解析
      整数**继续（容忍部分损坏），取不到则从 0 开始；任何 I/O 异常同样
      按损坏降级，**绝不抛异常崩溃**。
    """
    try:
        data = Path(path).read_text(encoding="utf-8")
    except OSError:
        return 0, "missing"
    try:
        obj = json.loads(data)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        raw_seq = obj.get("seq")
        if isinstance(raw_seq, bool):
            raw_seq = None
        if isinstance(raw_seq, int) and raw_seq >= 0:
            return int(raw_seq), "ok"
        if isinstance(raw_seq, float) and raw_seq.is_integer() and raw_seq >= 0:
            return int(raw_seq), "ok"
    # 损坏 / 格式不符：取最后一个可解析数值（含小数形态，如 "3.0" → 3）
    nums = re.findall(r"\d+(?:\.\d+)?", data)
    if nums:
        try:
            return max(0, int(float(nums[-1]))), "corrupt"
        except ValueError:
            pass
    return 0, "corrupt"


def save_seq_atomic(path: str | os.PathLike, seq: int) -> bool:
    """原子写盘：先写同目录临时文件再 ``os.replace`` 替换，避免半截文件。

    写盘失败返回 False（调用方降级为仅内存推进），不抛异常。
    """
    target = str(path)
    tmp = f"{target}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"seq": int(seq)}, f)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, target)
        return True
    except (OSError, ValueError):
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


# ----------------------------------------------------------------------
# 主题（对齐 docs/mqtt-topics.md 的 robot 通道）
# ----------------------------------------------------------------------
def cmd_topic(device_id: str) -> str:
    """平台 → 设备：通用命令（pause/resume/return_home/emergency_stop/ack）。"""
    return f"robot/{device_id}/cmd"


def task_topic(device_id: str) -> str:
    """平台 → 设备：派单（dispatch，对应后端 MqttClient.publish_task）。"""
    return f"robot/{device_id}/task"


def ack_topic(device_id: str) -> str:
    """设备 → 平台：命令回执（后端订阅 robot/+/cmd/ack）。"""
    return f"robot/{device_id}/cmd/ack"


def telemetry_topic(device_id: str) -> str:
    """设备 → 平台：遥测。"""
    return f"robot/{device_id}/telemetry"


# ----------------------------------------------------------------------
# 孪生配置
# ----------------------------------------------------------------------
@dataclass
class TwinConfig:
    """孪生物理/行为参数。默认值全部可覆盖，测试用极端值加速收敛。"""

    base_position: Position = field(default_factory=lambda: Position(119.6500, 26.3800))
    initial_battery: int = 100
    battery_drain_per_tick: float = 1.0
    battery_recharge_per_tick: float = 0.5
    battery_critical_threshold: int = 15
    bin_full_threshold: float = 0.95
    bin_fill_per_tick: float = 0.02
    speed_mps: float = 1.5
    travel_deg_per_tick: float = 0.0001
    arrive_radius_deg: float = 0.00002
    outbox_size: int = 1000
    command_processing_delay: float = 0.0
    bins: dict[str, float] = field(
        default_factory=lambda: {"foam": 0.0, "plastic": 0.0, "mixed": 0.0}
    )


# ----------------------------------------------------------------------
# 设备孪生
# ----------------------------------------------------------------------
class DeviceTwin:
    """一台机器人作业单元的确定性数字孪生。"""

    def __init__(
        self,
        device_id: str,
        transport: DeviceTransport | None = None,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        config: TwinConfig | None = None,
        event_sink: list[dict[str, Any]] | None = None,
        telemetry_modifier: Callable[[Telemetry], None] | None = None,
        seq_store_path: str | os.PathLike | None = None,
    ) -> None:
        self.device_id = device_id
        self.transport = transport or MemoryTransport()
        self.clock = clock or FakeClock(0.0)
        self.id_factory = id_factory or SequentialIdFactory("ack")
        self.config = config or TwinConfig()
        self.events: list[dict[str, Any]] = (
            event_sink if event_sink is not None else []
        )  # 与场景 runner 共享的事件流水
        #: 可选遥测修改器（场景噪声注入用）：在发布前对 Telemetry 就地修改。
        self.telemetry_modifier = telemetry_modifier

        # 主题
        self.cmd_topic = cmd_topic(device_id)
        self.task_topic = task_topic(device_id)
        self.ack_topic = ack_topic(device_id)
        self.telemetry_topic = telemetry_topic(device_id)

        # 物理状态
        self.mode: str = DeviceMode.IDLE
        self.battery: float = float(self.config.initial_battery)
        self.position: Position | None = Position(
            self.config.base_position.lng, self.config.base_position.lat
        )
        self.bins: dict[str, float] = dict(self.config.bins)
        self.mission_id: str | None = None
        self.mission_target: Position | None = None

        # 遥测序号（单调递增）。WP-14E：支持跨进程持久化 —— 路径可经构造
        # 参数 ``seq_store_path`` 或环境变量 DEVICE_SIM_SEQ_FILE 配置，
        # **默认不写盘**（None，保持既有测试与行为不变）。文件缺失/损坏
        # 以 warning 降级（从 0 或最后一个可解析值继续），绝不抛异常；
        # 落盘使用原子写（临时文件 + os.replace），重启后序号严格单调递增，
        # 不回退到 0、不重复已用序号。
        if seq_store_path is None:
            seq_store_path = os.environ.get(DEVICE_SIM_SEQ_FILE_ENV) or None
        self.seq_store_path: str | None = str(seq_store_path) if seq_store_path else None
        self.seq: int = 0
        if self.seq_store_path:
            restored, status = load_seq_from_file(self.seq_store_path)
            if status == "ok" and restored > 0:
                self.seq = restored
                logger.info(
                    "设备 %s 遥测序号从 %s 恢复为 %d（下一 tick 起严格递增）",
                    device_id, self.seq_store_path, restored,
                )
            elif status == "corrupt":
                # 损坏：从最后一个可解析值（或 0）继续，不抛异常
                self.seq = restored
                logger.warning(
                    "设备 %s 遥测序号文件损坏（%s），从 %d 继续（降级，不抛异常）",
                    device_id, self.seq_store_path, restored,
                )
            # status == "missing"：首次运行，正常从 0 开始

        # 命令/回执状态
        self._pending: list[DeviceCommand] = []
        self._batch_acks: list[Ack] = []
        self._acked: dict[str, Ack] = {}  # command_id -> 最近一次回执（幂等复回）
        self._mode_before_estop: str | None = None
        self._mode_before_pause: str | None = None

        # 故障状态
        self._fault_flags: set[str] = set()
        self._fault_lifecycle: dict[str, dict[str, float | None]] = {}

        # 断网 Outbox
        self._outbox: deque[tuple[str, dict[str, Any], int]] = deque(
            maxlen=self.config.outbox_size
        )
        self._was_connected: bool = bool(self.transport.connected)

        self._started = False
        self.history: dict[str, list[Any]] = {"acks": [], "telemetry": [], "commands": []}
        self.counter: dict[str, int] = {
            "commands_processed": 0,
            "commands_rejected": 0,
            "commands_invalid": 0,
            "acks_published": 0,
            "acks_suppressed": 0,
            "telemetry_built": 0,
            "telemetry_published": 0,
            "buffered": 0,
            "flushed": 0,
            "faults_injected": 0,
            "faults_recovered": 0,
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """订阅命令通道并发出初始遥测。"""
        if self._started:
            return
        self._started = True
        self.transport.subscribe(self.cmd_topic, self._on_command)
        self.transport.subscribe(self.task_topic, self._on_command)
        self.tick()

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self.transport.unsubscribe(self.cmd_topic, self._on_command)
        self.transport.unsubscribe(self.task_topic, self._on_command)

    # ------------------------------------------------------------------
    # 入站命令
    # ------------------------------------------------------------------
    def _on_command(self, topic: str, payload: dict[str, Any]) -> None:
        try:
            cmd = DeviceCommand.from_dict(payload)
        except (KeyError, TypeError, ValueError) as exc:
            self.counter["commands_invalid"] += 1
            self._event("command_invalid", topic=topic, error=str(exc))
            return
        if cmd.device_id != self.device_id:
            self.counter["commands_invalid"] += 1
            self._event("command_invalid", command_id=cmd.command_id, reason="device_id_mismatch")
            return
        self._pending.append(cmd)
        self._event("command_queued", command_id=cmd.command_id, action=cmd.action, seq=cmd.seq)

    def receive_command(self, command: DeviceCommand | dict[str, Any]) -> None:
        """直接注入一条命令（测试用，等价于 transport 回调路径）。"""
        if isinstance(command, dict):
            command = DeviceCommand.from_dict(command)
        self._pending.append(command)
        self._event("command_queued", command_id=command.command_id, action=command.action, seq=command.seq)

    # ------------------------------------------------------------------
    # 主循环：每 tick 处理一批命令 → 推进世界 → 发遥测
    # ------------------------------------------------------------------
    def tick(self) -> Telemetry:
        """推进一个确定性的 tick，返回本 tick 构造的遥测对象。

        顺序固定：1) 重连补传检查  2) 取命令批次  3) 执行并排队回执
        4) 发布批次回执（含故障行为）  5) 推进物理世界  6) 发遥测。
        """
        self._maybe_flush_outbox()
        now = self.clock.now()
        batch = self._drain_pending()
        self._batch_acks = []
        for cmd in batch:
            self._process_command(cmd, now)
        self._emit_batch_acks(now)
        self._step_world()
        return self._publish_telemetry()

    def _drain_pending(self) -> list[DeviceCommand]:
        """取出一批待处理命令：按 seq 升序，emergency_stop 无条件插队最前。"""
        if not self._pending:
            return []
        pending, self._pending = self._pending, []
        pending.sort(key=lambda c: c.seq)
        e_stops = [c for c in pending if c.action == "emergency_stop"]
        others = [c for c in pending if c.action != "emergency_stop"]
        return e_stops + others

    def _process_command(self, cmd: DeviceCommand, now: float) -> None:
        # 处理延迟：未到 `issued_at + processing_delay` 的命令留在队列，
        # 用于模拟设备响应慢导致的超时 ACK（晚于 expires_at 才回执）。
        if now < cmd.issued_at + self.config.command_processing_delay:
            self._pending.append(cmd)
            self._event(
                "command_deferred",
                command_id=cmd.command_id,
                until=round(cmd.issued_at + self.config.command_processing_delay, 6),
            )
            return

        self.counter["commands_processed"] += 1
        self.history["commands"].append(cmd)

        # 重复命令（同 command_id）→ 幂等：不重复执行，复回上次回执
        prev = self._acked.get(cmd.command_id)
        if prev is not None:
            self._batch_acks.append(prev)
            self._event("duplicate_command", command_id=cmd.command_id)
            return

        if cmd.action not in COMMAND_ACTIONS:
            self._store_ack(self._make_ack(cmd, False, "unknown_action", now))
            return

        if cmd.is_expired(now):
            self._store_ack(self._make_ack(cmd, False, "expired", now))
            self._event("command_expired", command_id=cmd.command_id, expires_at=cmd.expires_at)
            return

        if self.mode == DeviceMode.E_STOP and cmd.action not in (
            "emergency_stop",
            "resume",  # resume 是急停的唯一命令恢复路径，必须放行
        ):
            self._store_ack(self._make_ack(cmd, False, "device_in_emergency_stop", now))
            return

        accepted, reason = self._apply_action(cmd.action, cmd.params, now)
        self._store_ack(self._make_ack(cmd, accepted, reason, now))
        self._event(
            "command_applied",
            command_id=cmd.command_id,
            action=cmd.action,
            seq=cmd.seq,
            accepted=accepted,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # 动作执行（返回 (accepted, reason)）
    # ------------------------------------------------------------------
    def _apply_action(self, action: str, params: dict[str, Any], now: float) -> tuple[bool, str]:
        if action == "dispatch":
            return self._do_dispatch(params)
        if action == "pause":
            return self._do_pause()
        if action == "resume":
            return self._do_resume()
        if action == "return_home":
            return self._do_return_home()
        if action == "emergency_stop":
            return self._do_emergency_stop()
        if action == "ack":
            return True, "ack_echo"
        return False, "unknown_action"

    def _do_dispatch(self, params: dict[str, Any]) -> tuple[bool, str]:
        target = params.get("target")
        if not isinstance(target, dict) or "lng" not in target or "lat" not in target:
            return False, "missing_target"
        mission_id = params.get("task_id") or params.get("mission_id")
        if not mission_id:
            mission_id = f"msn_{int(self.clock.now())}_{self.seq}"
        self.mission_id = mission_id
        self.mission_target = Position(float(target["lng"]), float(target["lat"]))
        self.mode = DeviceMode.NAVIGATING
        self._event("mode_change", mode=self.mode, mission_id=mission_id)
        return True, "dispatched"

    def _do_pause(self) -> tuple[bool, str]:
        if self.mode in (DeviceMode.NAVIGATING, DeviceMode.COLLECTING, DeviceMode.RETURNING):
            self._mode_before_pause = self.mode
            self.mode = DeviceMode.PAUSED
            self._event("mode_change", mode=self.mode)
            return True, "paused"
        if self.mode == DeviceMode.PAUSED:
            return True, "already_paused"
        return True, "noop"

    def _do_resume(self) -> tuple[bool, str]:
        if self.mode == DeviceMode.E_STOP:
            self.mode = self._mode_before_estop or DeviceMode.NAVIGATING
            self._mode_before_estop = None
            self._auto_clear_fault("emergency_stop")
            self._event("mode_change", mode=self.mode, reason="recovered_from_emergency_stop")
            return True, "recovered_from_emergency_stop"
        if self.mode == DeviceMode.PAUSED:
            self.mode = self._mode_before_pause or DeviceMode.NAVIGATING
            self._mode_before_pause = None
            self._event("mode_change", mode=self.mode, reason="resumed")
            return True, "resumed"
        return True, "noop"

    def _do_return_home(self) -> tuple[bool, str]:
        if self.mode in (DeviceMode.NAVIGATING, DeviceMode.COLLECTING, DeviceMode.PAUSED):
            self.mode = DeviceMode.RETURNING
            self._event("mode_change", mode=self.mode)
            return True, "returning"
        if self.mode == DeviceMode.RETURNING:
            return True, "already_returning"
        if self.mode == DeviceMode.IDLE:
            return True, "already_home"
        return False, "device_in_emergency_stop"

    def _do_emergency_stop(self) -> tuple[bool, str]:
        if self.mode == DeviceMode.E_STOP:
            return True, "already_emergency_stop"
        self._mode_before_estop = self.mode
        self.mode = DeviceMode.E_STOP
        self._event("mode_change", mode=self.mode)
        return True, "emergency_stop"

    # ------------------------------------------------------------------
    # 回执
    # ------------------------------------------------------------------
    def _make_ack(self, cmd: DeviceCommand, accepted: bool, reason: str, now: float) -> Ack:
        return Ack(
            ack_id=self.id_factory.next(),
            command_id=cmd.command_id,
            device_id=self.device_id,
            seq=cmd.seq,
            received_at=now,
            accepted=accepted,
            reason=reason,
            mode=self.mode,
        )

    def _store_ack(self, ack: Ack) -> None:
        self._acked[ack.command_id] = ack
        self._batch_acks.append(ack)
        if not ack.accepted:
            self.counter["commands_rejected"] += 1

    def _emit_batch_acks(self, now: float) -> None:
        acks, self._batch_acks = self._batch_acks, []
        if not acks:
            return
        if "out_of_order_ack" in self._fault_flags and len(acks) > 1:
            acks = list(reversed(acks))  # 故障注入：回执按倒序发布
        for ack in acks:
            self._emit_ack(ack)

    def _emit_ack(self, ack: Ack) -> None:
        if "ack_timeout" in self._fault_flags:
            self.counter["acks_suppressed"] += 1
            self._event("ack_suppressed", command_id=ack.command_id, ack_id=ack.ack_id)
            return
        repeat = 2 if "duplicate_ack" in self._fault_flags else 1
        for _ in range(repeat):
            ok = self._publish_outbound(self.ack_topic, ack.to_dict(), qos=1, kind="ack")
            self.history["acks"].append(ack)
            self.counter["acks_published"] += 1
            self._event(
                "ack_published",
                command_id=ack.command_id,
                ack_id=ack.ack_id,
                accepted=ack.accepted,
                reason=ack.reason,
                delivered=ok,
            )

    # ------------------------------------------------------------------
    # 物理世界推进
    # ------------------------------------------------------------------
    def _step_world(self) -> None:
        if self.mode in (DeviceMode.NAVIGATING, DeviceMode.COLLECTING, DeviceMode.RETURNING):
            self.battery = max(0.0, self.battery - self.config.battery_drain_per_tick)
        else:
            self.battery = min(
                float(self.config.initial_battery),
                self.battery + self.config.battery_recharge_per_tick,
            )

        gps_ok = "gps_lost" not in self._fault_flags

        if self.mode == DeviceMode.NAVIGATING and self.mission_target is not None and gps_ok:
            self.position, arrived = self._move_toward(
                self.mission_target, self.config.travel_deg_per_tick
            )
            if arrived:
                self.mode = DeviceMode.COLLECTING
                self._event("mode_change", mode=self.mode, reason="arrived")
        elif self.mode == DeviceMode.COLLECTING:
            for key in self.bins:
                self.bins[key] = min(1.0, self.bins[key] + self.config.bin_fill_per_tick)
            if self._fault_active("bin_full") and self.mode == DeviceMode.COLLECTING:
                self.mode = DeviceMode.RETURNING
                self._event("mode_change", mode=self.mode, reason="bin_full_auto_return")
        elif self.mode == DeviceMode.RETURNING:
            self.position, arrived = self._move_toward(
                self.config.base_position, self.config.travel_deg_per_tick
            )
            if arrived:
                self._dump_bins()
                self.mission_id = None
                self.mission_target = None
                self.mode = DeviceMode.IDLE
                self._event("mode_change", mode=self.mode, reason="mission_complete")

        # 电量临界 → 自动返航（天然故障恢复闭环）
        if self._fault_active("battery_critical") and self.mode in (
            DeviceMode.NAVIGATING,
            DeviceMode.COLLECTING,
        ):
            self.mode = DeviceMode.RETURNING
            self._event("mode_change", mode=self.mode, reason="battery_critical_auto_return")

    def _move_toward(self, target: Position, step: float) -> tuple[Position, bool]:
        """向目标移动一步（经纬度），到达返回 (目标点, True)。"""
        if self.position is None:
            self.position = Position(self.config.base_position.lng, self.config.base_position.lat)
        d_lng = target.lng - self.position.lng
        d_lat = target.lat - self.position.lat
        dist = (d_lng * d_lng + d_lat * d_lat) ** 0.5
        if dist <= self.config.arrive_radius_deg or step >= dist:
            self.position = Position(target.lng, target.lat)
            return self.position, True
        k = step / dist
        self.position = Position(
            self.position.lng + d_lng * k, self.position.lat + d_lat * k
        )
        return self.position, False

    def _dump_bins(self) -> None:
        for key in self.bins:
            self.bins[key] = 0.0

    def _bin_total(self) -> float:
        return float(sum(self.bins.values()))

    # ------------------------------------------------------------------
    # 故障
    # ------------------------------------------------------------------
    def _fault_active(self, code: str) -> bool:
        """故障是否生效：注入标志，或天然条件（低电量 / 仓满）。"""
        if code in self._fault_flags:
            return True
        if code == "battery_critical":
            return self.battery < self.config.battery_critical_threshold
        if code == "bin_full":
            return self._bin_total() >= self.config.bin_full_threshold
        return False

    def _resolve_fault_code(self) -> str:
        """当前主故障码：按 FAULT_PRIORITY 取最高优先级的生效故障。"""
        if self.mode == DeviceMode.E_STOP:
            return "emergency_stop"
        for code in FAULT_PRIORITY:
            if code == "emergency_stop":
                continue
            if self._fault_active(code):
                return code
        return FAULT_NONE

    @property
    def fault_code(self) -> str:
        return self._resolve_fault_code()

    def inject_fault(self, code: str) -> bool:
        """注入故障（含行为副作用）。已在生效则返回 False。"""
        if code not in FAULT_CODES:
            raise ValueError(f"未知故障码: {code}")
        lc = self._fault_lifecycle.get(code)
        if lc is not None and lc.get("cleared_at") is None:
            return False  # 已处于生效状态，不重复注入
        self._fault_flags.add(code)
        self._fault_lifecycle[code] = {"injected_at": self.clock.now(), "cleared_at": None}
        self.counter["faults_injected"] += 1
        self._event("fault_injected", fault=code)
        if code == "communication_lost":
            self.transport.disconnect()
            self._was_connected = False
        if code == "emergency_stop" and self.mode != DeviceMode.E_STOP:
            self._mode_before_estop = self.mode
            self.mode = DeviceMode.E_STOP
            self._event("mode_change", mode=self.mode, reason="fault_emergency_stop")
        return True

    def clear_fault(self, code: str) -> bool:
        """清除注入故障；返回是否确实处于生效状态。"""
        if code not in FAULT_CODES:
            raise ValueError(f"未知故障码: {code}")
        was_active = code in self._fault_flags
        if was_active:
            self._fault_flags.discard(code)
            lc = self._fault_lifecycle.get(code)
            if lc is not None and lc.get("cleared_at") is None:
                lc["cleared_at"] = self.clock.now()
                self.counter["faults_recovered"] += 1
                self._event("fault_recovered", fault=code, source="injector")
            if code == "communication_lost":
                self.transport.connect()
            if code == "emergency_stop" and self.mode == DeviceMode.E_STOP:
                self.mode = self._mode_before_estop or DeviceMode.NAVIGATING
                self._mode_before_estop = None
                self._event("mode_change", mode=self.mode, reason="fault_emergency_stop_cleared")
        return was_active

    def _auto_clear_fault(self, code: str) -> None:
        """命令驱动自动恢复（如 resume 解除 emergency_stop 故障）。"""
        if code in self._fault_flags:
            self._fault_flags.discard(code)
            lc = self._fault_lifecycle.get(code)
            if lc is not None and lc.get("cleared_at") is None:
                lc["cleared_at"] = self.clock.now()
                self.counter["faults_recovered"] += 1
                self._event("fault_recovered", fault=code, source="command")

    # ------------------------------------------------------------------
    # 遥测与出站
    # ------------------------------------------------------------------
    def _build_telemetry(self) -> Telemetry:
        self.seq += 1
        # WP-14E：配置了持久化路径时，每次自增后原子落盘（临时文件 + 替换）。
        # 落盘失败降级为仅内存推进（下个 tick 重试），不打断遥测闭环。
        if self.seq_store_path and not save_seq_atomic(self.seq_store_path, self.seq):
            logger.warning(
                "设备 %s 遥测序号落盘失败（%s），本次仅内存推进",
                self.device_id, self.seq_store_path,
            )
        velocity = (
            self.config.speed_mps
            if self.mode in (DeviceMode.NAVIGATING, DeviceMode.COLLECTING, DeviceMode.RETURNING)
            else 0.0
        )
        position = None if "gps_lost" in self._fault_flags else self.position
        return Telemetry(
            device_id=self.device_id,
            seq=self.seq,
            timestamp=self.clock.now(),
            mode=self.mode,
            battery=int(round(self.battery)),
            position=position,
            velocity=velocity,
            bin_usage=dict(self.bins),
            mission_id=self.mission_id,
            fault_code=self._resolve_fault_code(),
        )

    def _publish_telemetry(self) -> Telemetry:
        telemetry = self._build_telemetry()
        if self.telemetry_modifier is not None:
            self.telemetry_modifier(telemetry)
        ok = self._publish_outbound(
            self.telemetry_topic, telemetry.to_dict(), qos=0, kind="telemetry"
        )
        self.history["telemetry"].append(telemetry)
        self.counter["telemetry_built"] += 1
        if ok:
            self.counter["telemetry_published"] += 1
        return telemetry

    def _publish_outbound(
        self, topic: str, payload: dict[str, Any], qos: int, *, kind: str
    ) -> bool:
        """出站发布：在线直发，离线进入 Outbox 排队（断网排队）。"""
        if self.transport.connected:
            return bool(self.transport.publish(topic, payload, qos=qos))
        self._outbox.append((topic, payload, qos))
        self.counter["buffered"] += 1
        self._event("buffered", kind=kind, topic=topic, seq=payload.get("seq"))
        return False

    # ------------------------------------------------------------------
    # 断网排队 / 重连补传
    # ------------------------------------------------------------------
    def _maybe_flush_outbox(self) -> None:
        if not self.transport.connected:
            self._was_connected = False
            return
        if self._was_connected:
            return
        self._was_connected = True
        sent = self._flush_outbox()
        if sent:
            self._event("outbox_flushed", count=sent)

    def _flush_outbox(self, batch: int | None = None) -> int:
        """按原顺序补传 Outbox；中途再次断线则剩余留在队首。"""
        sent = 0
        while self._outbox:
            topic, payload, qos = self._outbox.popleft()
            if not self.transport.publish(topic, payload, qos=qos):
                self._outbox.appendleft((topic, payload, qos))
                break
            sent += 1
            self.counter["flushed"] += 1
            if batch is not None and sent >= batch:
                break
        return sent

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """当前状态快照（不产生副作用）。"""
        return {
            "device_id": self.device_id,
            "mode": self.mode,
            "battery": int(round(self.battery)),
            "position": self.position.to_dict() if self.position is not None else None,
            "velocity": (
                self.config.speed_mps
                if self.mode
                in (DeviceMode.NAVIGATING, DeviceMode.COLLECTING, DeviceMode.RETURNING)
                else 0.0
            ),
            "bin_usage": {k: round(v, 4) for k, v in self.bins.items()},
            "mission_id": self.mission_id,
            "fault_code": self._resolve_fault_code(),
            "seq": self.seq,
            "pending_commands": len(self._pending),
            "outbox_size": len(self._outbox),
            "connected": bool(self.transport.connected),
        }

    def _event(self, etype: str, **kw: Any) -> None:
        self.events.append({"t": round(self.clock.now(), 6), "type": etype, **kw})


__all__ = [
    "cmd_topic",
    "task_topic",
    "ack_topic",
    "telemetry_topic",
    "DEVICE_SIM_SEQ_FILE_ENV",
    "load_seq_from_file",
    "save_seq_atomic",
    "TwinConfig",
    "DeviceTwin",
]
