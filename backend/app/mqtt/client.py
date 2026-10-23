"""MQTT 客户端：订阅设备上行消息，分发到处理器。

使用 aiomqtt（异步），在 FastAPI lifespan 中以 asyncio task 运行。
注意：不要在 MQTT 回调里直接做阻塞 DB 操作——通过 asyncio 调度。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

import aiomqtt
from loguru import logger

from app.core.config import settings
from app.mqtt.ack import AckTracker
from app.mqtt.topics import SUBSCRIBE_PLAN, Topics

#: 派单命令默认有效期（秒）。与孪生侧默认 TTL（edge/device_sim/faults.py
#: `DEFAULT_COMMAND_TTL = 30.0`）对齐；可通过 `publish_task(ttl=...)` 覆盖，
#: 不引入全局配置项（docs/device-interface.md §3 / §9）。
DISPATCH_TTL_SECONDS = 30.0


def _iso_utc(epoch: float) -> str:
    """epoch 秒 → UTC ISO-8601 字符串（冻结信封的 `*_iso` 伴随字段）。"""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def command_id_for_task(task_id: str) -> str:
    """派单命令幂等键：稳定派生自 task_id。

    同一任务重复下发（MQTT QoS1 重投、ACK 超时换车重派同一任务等）会复用
    同一个 command_id —— 设备侧按 command_id 幂等去重，重复命令只执行一次。
    """
    return f"cmd_{task_id}"


def build_dispatch_payload(
    *,
    robot_id: str,
    task_id: str,
    lng: float,
    lat: float,
    priority: int,
    seq: int,
    now: float | None = None,
    ttl: float = DISPATCH_TTL_SECONDS,
) -> dict[str, Any]:
    """构造冻结命令信封兼容的派单报文（纯函数，时钟可注入）。

    报文同时携带：
      - 冻结信封字段（docs/device-interface.md §3）：``command_id`` /
        ``device_id`` / ``seq`` / ``issued_at`` / ``expires_at`` /
        ``action`` / ``params``（params 内含 task_id / target / priority）；
      - 向后兼容顶层字段：``task_id`` / ``target`` / ``priority`` 与
        ``issued_at``（信封字段，epoch 秒；``issued_at_iso`` 为 ISO 副本）。

    ``now`` 可注入（测试用确定性时钟，不用随机数）；``expires_at = issued_at + ttl``。
    """
    issued = now if now is not None else time.time()
    expires = issued + ttl
    params = {
        "task_id": task_id,
        "target": {"lng": float(lng), "lat": float(lat)},
        "priority": int(priority),
    }
    return {
        "command_id": command_id_for_task(task_id),
        "device_id": robot_id,
        "seq": int(seq),
        "action": "dispatch",
        "issued_at": issued,
        "issued_at_iso": _iso_utc(issued),
        "expires_at": expires,
        "expires_at_iso": _iso_utc(expires),
        "params": params,
        # ---- 向后兼容顶层字段（既有消费者可继续按原字段名读取）----
        "task_id": task_id,
        "target": params["target"],
        "priority": int(priority),
    }


class SeqAllocator:
    """进程内按 device_id 维护单调递增的命令序号（并发安全）。

    序号是派单报文 ``seq`` 的唯一真源：同设备必须严格递增（冻结信封要求
    平台侧单调），不同设备各自独立计数。分配在锁内同步完成，多个并发
    派单任务不会互相覆盖；不使用随机数，测试可复现。
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._lock = threading.Lock()

    def next(self, device_id: str) -> int:
        with self._lock:
            seq = self._counters.get(device_id, 0) + 1
            self._counters[device_id] = seq
            return seq


class MqttClient:
    """MQTT 客户端封装。"""

    def __init__(self) -> None:
        self._client: aiomqtt.Client | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._connected = False
        self._handlers: dict[str, Any] = {}
        self._seq_allocator = SeqAllocator()
        #: ACK 幂等跟踪（WP-14C）：publish_task 发布成功后登记命令，
        #: handle_robot_ack 消费时按 command_id 去重 / late / 乱序判定。
        self._ack_tracker = AckTracker()

    @property
    def ack_tracker(self) -> AckTracker:
        """进程内 ACK 跟踪表（与 publish_task 共用同一实例）。"""
        return self._ack_tracker

    @property
    def is_connected(self) -> bool:
        """当前是否真的连上了 Broker。

        用于健康检查 —— 只看 `_running` 是不够的：`start()` 之后
        `_running` 立刻为 True，但此时可能还在重连、根本没握上手，
        健康检查会误报"正常"。
        """
        return self._running and self._connected

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self, handlers: dict[str, Any]) -> None:
        """启动订阅循环（非阻塞）。"""
        self._handlers = handlers
        self._running = True
        self._task = asyncio.create_task(self._run_forever())
        logger.info("[MQTT] 客户端已启动")

    async def stop(self) -> None:
        """停止订阅。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[MQTT] 客户端已停止")

    # ------------------------------------------------------------------
    # 订阅循环（含断线重连）
    # ------------------------------------------------------------------
    async def _run_forever(self) -> None:
        """持续订阅，断线自动重连。"""
        while self._running:
            try:
                async with aiomqtt.Client(
                    hostname=settings.mqtt_host,
                    port=settings.mqtt_port,
                    username=settings.mqtt_username or None,
                    password=settings.mqtt_password or None,
                    identifier=settings.mqtt_client_id,
                    keepalive=settings.mqtt_keepalive,
                ) as client:
                    self._client = client
                    self._connected = True
                    logger.info(
                        f"[MQTT] 已连接 {settings.mqtt_host}:{settings.mqtt_port}"
                    )

                    # 订阅全部上行主题
                    for topic in SUBSCRIBE_PLAN:
                        await client.subscribe(topic, qos=1)
                        logger.info(f"[MQTT] 订阅 {topic}")

                    # 消息循环
                    async for message in client.messages:
                        try:
                            await self._dispatch(message)
                        except Exception as exc:   # noqa: BLE001
                            logger.exception(f"[MQTT] 消息处理异常：{exc}")

            except aiomqtt.MqttError as exc:
                self._connected = False
                if not self._running:
                    break
                logger.warning(f"[MQTT] 连接断开（{exc}），5 秒后重连")
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                self._connected = False
                break
            except Exception as exc:   # noqa: BLE001
                self._connected = False
                logger.exception(f"[MQTT] 未预期错误：{exc}")
                await asyncio.sleep(5)
            finally:
                # 退出 async with 后连接必然失效，别让 is_connected 假阳性
                self._connected = False

    # ------------------------------------------------------------------
    # 消息分发
    # ------------------------------------------------------------------
    async def _dispatch(self, message: aiomqtt.Message) -> None:
        """按主题路由到对应处理器。"""
        topic = str(message.topic)
        try:
            payload = json.loads(message.payload) if message.payload else {}
        except json.JSONDecodeError:
            logger.warning(f"[MQTT] 非法 JSON payload，topic={topic}")
            return

        # 通配主题 → handler 名
        handler_name = self._match_handler(topic)
        if handler_name is None:
            logger.debug(f"[MQTT] 无处理器匹配：{topic}")
            return

        handler = self._handlers.get(handler_name)
        if handler is None:
            logger.warning(f"[MQTT] 处理器未注册：{handler_name}")
            return

        await handler(topic, payload)

    def _match_handler(self, topic: str) -> str | None:
        """把具体主题映射到 SUBSCRIBE_PLAN 中声明的处理器。

        ★ 判据必须是**结构**（第 n 段是什么），不能是**总段数**。
        ────────────────────────────────────────────────────────
        这里踩过一个把整条作业链路掐断的坑：

            robot/{robot_id}/task/progress  → 4 段，parts[3] == "progress"
            robot/{robot_id}/cmd/ack        → 4 段，parts[3] == "ack"

        两个主题**段数完全相同**。早期用 `len(parts) == 5` 去认 cmd/ack，
        该条件永远不成立（通配符 `robot/+/cmd/ack` 展开就是 4 段），
        于是 ACK 消息一路落到 `return None`，被静默丢弃。

        后果不是"报个错"，而是：机器人回了 ACK → 平台没收到 →
        任务永远停在 assigned → 大屏上工单卡住不动。
        而 handle_robot_ack 本身写得好好的，测试也是绿的 ——
        **它只是永远不会被调用**。这类"路由不到"的缺陷没有任何日志，
        只有端到端演示时才以"业务不动了"的形式浮现。

        正确做法：逐段比对固定位置的常量，长度只在必要时做下界校验。
        """
        parts = topic.split("/")

        # ---- marine/{site}/{device}/<suffix> ----
        if len(parts) == 4 and parts[0] == "marine":
            suffix = parts[3]
            return {
                "event": "handle_event",
                "telemetry": "handle_telemetry",
                "status": "handle_device_status",
            }.get(suffix)

        # ---- robot/{robot_id}/<固定路径> ----
        # 注意：不判断总段数，而是看机器人 ID 之后的那两段
        if len(parts) == 4 and parts[0] == "robot":
            if parts[2] == "task" and parts[3] == "progress":
                return "handle_robot_progress"
            if parts[2] == "cmd" and parts[3] == "ack":
                return "handle_robot_ack"

        return None

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------
    async def publish(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        qos: int = 1,
        retain: bool = False,
    ) -> bool:
        """发布消息（下发指令、派单）。"""
        if self._client is None:
            logger.error(f"[MQTT] 客户端未连接，发布失败：{topic}")
            return False
        try:
            await self._client.publish(
                topic,
                payload=json.dumps(payload, ensure_ascii=False, default=str),
                qos=qos,
                retain=retain,
            )
            logger.debug(f"[MQTT] 发布 {topic}")
            return True
        except Exception as exc:   # noqa: BLE001
            logger.error(f"[MQTT] 发布失败 {topic}：{exc}")
            return False

    async def publish_task(
        self,
        robot_id: str,
        task_id: str,
        lng: float,
        lat: float,
        priority: int,
        *,
        ttl: float = DISPATCH_TTL_SECONDS,
    ) -> bool:
        """下发派单任务（冻结命令信封兼容报文，docs/device-interface.md §3）。

        信封字段：``command_id``（= cmd_{task_id}，稳定幂等）、``device_id``、
        ``seq``（同设备进程内单调递增）、``issued_at``（epoch 秒）、
        ``expires_at``（默认 issued_at + 30s，可经 ``ttl`` 覆盖）、
        ``action="dispatch"``、``params{task_id,target,priority}``。
        顶层同时保留向后兼容字段 ``task_id`` / ``target`` / ``priority``。
        旧调用方签名不变，无需修改。

        WP-14C：**仅在 MQTT 发布成功后**把
        ``command_id / device_id / seq / expires_at`` 登记到 AckTracker，
        供 ACK 的 late（超时）判定。登记表是进程内内存：发布失败、进程
        重启后未登记的命令仍可正常接收，但不会伪判 late（见
        ``app.mqtt.ack.AckTracker`` 的回落策略）。
        """
        seq = self._seq_allocator.next(robot_id)
        payload = build_dispatch_payload(
            robot_id=robot_id,
            task_id=task_id,
            lng=lng,
            lat=lat,
            priority=priority,
            seq=seq,
            ttl=ttl,
        )
        ok = await self.publish(Topics.robot_task(robot_id), payload, qos=1)
        if ok:
            self._ack_tracker.register_command(
                command_id=payload["command_id"],
                device_id=payload["device_id"],
                seq=payload["seq"],
                expires_at=payload["expires_at"],
            )
        return ok


# 全局单例
mqtt_client = MqttClient()
