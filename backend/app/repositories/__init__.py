"""数据访问层：把 SQL 与空间查询收敛在此，隔离 ORM 细节。

设计原则：service 层不直接写 SQL，repository 层不写业务逻辑。
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from geoalchemy2 import Geography
from geoalchemy2.functions import ST_AsText, ST_DWithin, ST_MakePoint, ST_SetSRID, ST_SnapToGrid, ST_Transform, ST_X, ST_Y
from sqlalchemy import Float, Integer, String, and_, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.models.event import Event, EventStatus, WasteClass
from app.models.task import Task, TaskStatus
from app.models.task_ack import TaskAck
from app.mqtt.ack import AckEnvelope, AckResult


class DeviceRepository:
    """设备数据访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_device_id(self, device_id: str) -> Device | None:
        result = await self.session.execute(
            select(Device).where(Device.device_id == device_id)
        )
        return result.scalar_one_or_none()

    async def list_devices(
        self,
        device_type: str | None = None,
        status: str | None = None,
    ) -> list[Device]:
        stmt = select(Device).order_by(Device.device_id)
        if device_type:
            stmt = stmt.where(Device.device_type == device_type)
        if status:
            stmt = stmt.where(Device.status == status)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_robots(self) -> list[Device]:
        return await self.list_devices(device_type="robot")

    async def upsert_heartbeat(
        self,
        device_id: str,
        status: str | None = None,
    ) -> None:
        """更新心跳时间与状态（设备上线/下线）。"""
        device = await self.get_by_device_id(device_id)
        if device is None:
            return
        device.last_heartbeat = datetime.now()
        if status:
            device.status = status

    async def find_nearest_available_robots(
        self,
        event_id: str,
        *,
        max_distance_m: int = 5000,
        limit: int = 5,
        exclude_robot_ids: set[str] | None = None,
    ) -> list[tuple[Device, float]]:
        """KNN 就近查找可用机器人（锚点 = **事件**坐标）。

        返回：[(设备, 距离米), ...] 按距离升序。
        """
        event_geo = (
            select(Event.location)
            .where(Event.event_id == event_id)
            .scalar_subquery()
        )
        return await self._nearest_robots_to(
            event_geo,
            max_distance_m=max_distance_m,
            limit=limit,
            exclude_robot_ids=exclude_robot_ids,
        )

    async def find_nearest_available_robots_at(
        self,
        lng: float,
        lat: float,
        *,
        max_distance_m: int = 5000,
        limit: int = 5,
        exclude_robot_ids: set[str] | None = None,
    ) -> list[tuple[Device, float]]:
        """KNN 就近查找可用机器人（锚点 = **任意经纬度**）。

        ★ 为什么需要这个入口（而不是复用上面那个）：
            上面那版以 `event_id` 反查坐标（`select(Event.location)…` 子查询），
            而**人工建单**产生的工单 `event_id IS NULL`（见 `POST /tasks`），
            没有事件可反查 —— 补派时那类工单会被整体漏掉。
            坐标本来就存在 `t_task.target_location` 里，直接传进来即可，
            不必为此伪造一条事件。
        """
        point = ST_SetSRID(ST_MakePoint(lng, lat), 4326)
        return await self._nearest_robots_to(
            point,
            max_distance_m=max_distance_m,
            limit=limit,
            exclude_robot_ids=exclude_robot_ids,
        )

    async def _nearest_robots_to(
        self,
        anchor: Any,
        *,
        max_distance_m: int = 5000,
        limit: int = 5,
        exclude_robot_ids: set[str] | None = None,
    ) -> list[tuple[Device, float]]:
        """KNN 查询的**唯一实现**：锚点可以是子查询、也可以是常量点。

        使用 ST_DWithin 走 GiST 索引粗筛 + KNN 算子 <-> 排序，
        比 ORDER BY ST_Distance(...) 快得多（后者无法走索引）。

        `exclude_robot_ids`：一次性补派多张工单时，把本轮已派出去的机器人
        排除掉 —— 否则同一条 `status='online'` 的查询会反复返回同一台，
        多张工单全部压给一台机器人。
        """
        anchor_geo = cast(anchor, Geography)
        distance = func.ST_Distance(cast(Device.location, Geography), anchor_geo).label("dist_m")

        conditions = [
            Device.device_type == "robot",
            Device.status == "online",
            ST_DWithin(cast(Device.location, Geography), anchor_geo, max_distance_m),
        ]
        if exclude_robot_ids:
            conditions.append(Device.device_id.notin_(sorted(exclude_robot_ids)))

        stmt = (
            select(Device, distance)
            .where(and_(*conditions))
            .order_by(Device.location.op("<->")(anchor))   # KNN 算子
            .limit(limit)
        )

        rows = await self.session.execute(stmt)
        return [(row[0], float(row[1])) for row in rows.all()]

    async def find_robot_by_id(self, robot_id: str) -> Device | None:
        """按编号查机器人（校验类型）。"""
        device = await self.get_by_device_id(robot_id)
        if device is not None and device.device_type != "robot":
            return None
        return device

    # 别名：语义更清晰
    get_robot_by_id = find_robot_by_id

    async def count_by_status(self) -> dict[str, int]:
        """按状态统计设备数（大屏指标用）。"""
        stmt = select(Device.status, func.count()).group_by(Device.status)
        rows = await self.session.execute(stmt)
        return {row[0]: int(row[1]) for row in rows.all()}


class EventRepository:
    """事件数据访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **fields: Any) -> Event:
        event = Event(**fields)
        self.session.add(event)
        await self.session.flush()
        return event

    async def get_by_event_id(self, event_id: str) -> Event | None:
        result = await self.session.execute(
            select(Event).where(Event.event_id == event_id)
        )
        return result.scalar_one_or_none()

    async def exists_by_device_seq(self, device_id: str, seq: int) -> bool:
        """判重：同一设备同一序号是否已存在（防 MQTT 重传重复派单）。"""
        result = await self.session.execute(
            select(func.count()).select_from(Event).where(
                and_(Event.device_id == device_id, Event.seq == seq)
            )
        )
        return int(result.scalar_one()) > 0

    async def list_events(
        self,
        *,
        hours: int = 24,
        main_class: str | None = None,
        status: str | None = None,
        device_id: str | None = None,
        township: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Event], int]:
        since = datetime.now() - timedelta(hours=hours)
        conditions = [Event.event_time >= since]
        if main_class:
            conditions.append(Event.main_class == main_class)
        if status:
            conditions.append(Event.status == status)
        if device_id:
            conditions.append(Event.device_id == device_id)
        if township:
            conditions.append(Event.township == township)

        count_stmt = select(func.count()).select_from(Event).where(and_(*conditions))
        total = int((await self.session.execute(count_stmt)).scalar_one())

        stmt = (
            select(Event)
            .where(and_(*conditions))
            .order_by(Event.event_time.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = await self.session.execute(stmt)
        return list(rows.scalars().all()), total

    async def heatmap_aggregate(
        self,
        *,
        hours: int = 24,
        grid_size: int = 500,
        main_class: str | None = None,
    ) -> list[dict[str, Any]]:
        """热力图网格聚合。

        关键：必须投影到 3857（米制），否则 ST_SnapToGrid 单位是「度」。
        返回密度而非总数 —— 否则大网格天然比小网格热，图会失真。
        """
        since = datetime.now() - timedelta(hours=hours)
        area_m2 = float(grid_size * grid_size)

        projected = ST_Transform(Event.location, 3857)
        grid = ST_SnapToGrid(projected, grid_size).label("grid")

        conditions = [Event.event_time >= since, Event.status != EventStatus.IGNORED]
        if main_class:
            conditions.append(Event.main_class == main_class)

        subq = (
            select(
                grid,
                func.count().label("cnt"),
                (func.count() / area_m2).label("density"),
                func.mode().within_group(Event.main_class).label("main_class"),
            )
            .where(and_(*conditions))
            .group_by(grid)
            .subquery()
        )

        stmt = select(
            ST_X(ST_Transform(subq.c.grid, 4326)).label("lng"),
            ST_Y(ST_Transform(subq.c.grid, 4326)).label("lat"),
            subq.c.cnt,
            subq.c.density,
            subq.c.main_class,
        ).order_by(subq.c.cnt.desc()).limit(2000)

        rows = await self.session.execute(stmt)
        return [
            {
                "lng": float(r.lng),
                "lat": float(r.lat),
                "count": int(r.cnt),
                "density": float(r.density),
                "main_class": r.main_class,
            }
            for r in rows.all()
            if r.lng is not None and r.lat is not None
        ]

    async def count_since(self, hours: int = 24, township: str | None = None) -> int:
        since = datetime.now() - timedelta(hours=hours)
        stmt = select(func.count()).select_from(Event).where(Event.event_time >= since)
        if township:
            stmt = stmt.where(Event.township == township)
        return int((await self.session.execute(stmt)).scalar_one())

    async def list_main_classes_for_events(self, event_ids: list[str]) -> set[str]:
        """批量取多条事件的主类别。

        供派单引擎做「类别匹配加权」：需要比对本次事件与机器人
        在途任务所属事件的类别是否相同。**必须批量查**，
        不能放进循环里逐条查（候选机器人 × 在途任务 = N×M 次查询）。
        """
        if not event_ids:
            return set()
        stmt = select(Event.main_class).where(Event.event_id.in_(event_ids))
        rows = await self.session.execute(stmt)
        return {r[0] for r in rows.all() if r[0] is not None}

    async def recent_for_dispatch(self, limit: int = 20) -> list[Event]:
        """取最近待派单的高优先级事件。"""
        stmt = (
            select(Event)
            .where(
                and_(
                    Event.status == EventStatus.NEW,
                    Event.main_class.in_(WasteClass.HIGH_PRIORITY),
                )
            )
            .order_by(Event.event_time.asc())   # 先到先处理
            .limit(limit)
        )
        rows = await self.session.execute(stmt)
        return list(rows.scalars().all())


class TaskRepository:
    """任务数据访问。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **fields: Any) -> Task:
        task = Task(**fields)
        self.session.add(task)
        await self.session.flush()
        return task

    async def get_by_task_id(self, task_id: str) -> Task | None:
        result = await self.session.execute(
            select(Task).where(Task.task_id == task_id)
        )
        return result.scalar_one_or_none()

    async def has_active_task_for_event(self, event_id: str) -> bool:
        """防重复派单：该事件是否已有未完成任务。"""
        stmt = select(func.count()).select_from(Task).where(
            and_(
                Task.event_id == event_id,
                Task.status.in_(TaskStatus.ACTIVE),
            )
        )
        return int((await self.session.execute(stmt)).scalar_one()) > 0

    async def find_mergeable_task(
        self,
        *,
        lng: float,
        lat: float,
        window_minutes: int = 10,
        radius_m: int = 200,
    ) -> Task | None:
        """防抖合并：查找附近时间窗内的活跃任务，避免机器人被反复派往同一片水域。

        注意：合并逻辑必须在派单引擎层做，因为需要知道「当时有没有在途任务」。
        """
        since = datetime.now() - timedelta(minutes=window_minutes)
        point = ST_SetSRID(ST_MakePoint(lng, lat), 4326)

        stmt = (
            select(Task)
            .where(
                and_(
                    Task.status.in_(TaskStatus.ACTIVE),
                    Task.created_at >= since,
                    ST_DWithin(
                        cast(Task.target_location, Geography),
                        cast(point, Geography),
                        radius_m,
                    ),
                )
            )
            .order_by(Task.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_tasks(
        self,
        *,
        status: str | None = None,
        robot_id: str | None = None,
        township: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Task], int]:
        conditions = []
        if status:
            conditions.append(Task.status == status)
        if robot_id:
            conditions.append(Task.robot_id == robot_id)
        if township:
            conditions.append(Task.township == township)

        count_stmt = select(func.count()).select_from(Task)
        if conditions:
            count_stmt = count_stmt.where(and_(*conditions))
        total = int((await self.session.execute(count_stmt)).scalar_one())

        stmt = select(Task).order_by(Task.created_at.desc()).limit(limit).offset(offset)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        rows = await self.session.execute(stmt)
        return list(rows.scalars().all()), total

    async def list_active_tasks_for_robot(self, robot_id: str) -> list[Task]:
        stmt = select(Task).where(
            and_(Task.robot_id == robot_id, Task.status.in_(TaskStatus.ACTIVE))
        )
        rows = await self.session.execute(stmt)
        return list(rows.scalars().all())

    async def list_by_status(self, status: str, limit: int = 50) -> list[Task]:
        """按状态取任务（供 ACK 超时回退与换车重派扫描）。"""
        stmt = select(Task).where(Task.status == status).limit(limit)
        rows = await self.session.execute(stmt)
        return list(rows.scalars().all())

    async def list_unassigned_pending(self, limit: int = 50) -> list[Task]:
        """取「待派单 **且** 没有机器人」的工单 —— 补派扫描用。

        ★ 两个条件缺一不可，`robot_id IS NULL` 不是冗余：

            - `pending + robot_id IS NULL`  → **人工建单**（`POST /tasks`
              未指定机器人）或曾经派单失败留下的工单，**从没被派出去过**，
              该由补派去认领（本方法）；
            - `pending + robot_id IS NOT NULL` → ACK 超时被 `handle_ack_timeout`
              退回的工单，它**有**原定机器人，只能走 `reassign_task`
              （换车重派）。在这里再派一次会把它重复指派、并覆盖
              `assigned_at`，让 ACK 超时判定失去计时起点。

        排序：先紧急（priority 数值小，见 `TaskPriority`）后创建时间 ——
        与事件补派的「先到先处理」保持一致的语义。
        """
        stmt = (
            select(Task)
            .where(and_(Task.status == TaskStatus.PENDING, Task.robot_id.is_(None)))
            .order_by(Task.priority.asc(), Task.created_at.asc())
            .limit(limit)
        )
        rows = await self.session.execute(stmt)
        return list(rows.scalars().all())

    async def count_by_status(self, township: str | None = None) -> dict[str, int]:
        stmt = select(Task.status, func.count()).group_by(Task.status)
        if township:
            stmt = stmt.where(Task.township == township)
        rows = await self.session.execute(stmt)
        return {row[0]: int(row[1]) for row in rows.all()}

    async def pending_count(self) -> int:
        stmt = select(func.count()).select_from(Task).where(
            Task.status == TaskStatus.PENDING
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def done_count_since(self, hours: int = 24, township: str | None = None) -> int:
        since = datetime.now() - timedelta(hours=hours)
        stmt = select(func.count()).select_from(Task).where(
            and_(Task.status == TaskStatus.DONE, Task.finished_at >= since)
        )
        if township:
            stmt = stmt.where(Task.township == township)
        return int((await self.session.execute(stmt)).scalar_one())

    async def sum_collected_weight(self, township: str | None = None) -> float:
        stmt = select(func.coalesce(func.sum(Task.collected_weight), 0))
        if township:
            stmt = stmt.where(Task.township == township)
        return float((await self.session.execute(stmt)).scalar_one())


def _as_utc(value: datetime) -> datetime:
    """兼容历史 naive 时间戳（SQLite 读回为 naive），统一按 UTC 解释。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class TaskAckRepository:
    """ACK 审计账本数据访问（WP-14D，t_task_ack）。

    职责：把 WP-14C 的内存 ACK 判定落成可恢复、可查询的审计账本 ——
    每个 ``command_id`` 保留一条首次规范回执，后续重复只累计
    ``duplicate_count`` 并更新 ``last_duplicate_at / last_payload``，
    绝不覆盖首次规范回执（``raw_payload`` 只写一次）。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_command_id(self, command_id: str) -> TaskAck | None:
        result = await self.session.execute(
            select(TaskAck).where(TaskAck.command_id == command_id)
        )
        return result.scalar_one_or_none()

    async def record_arrival(
        self,
        *,
        command_id: str,
        task_id: str,
        device_id: str,
        seq: int,
        outcome: str,
        accepted: bool,
        reason: str,
        mode: str,
        received_at: datetime,
        raw_payload: dict[str, Any],
        received_wall_at: datetime | None = None,
    ) -> str:
        """登记一条回执到达，返回本次实际判定的结果（首次=``outcome``，重复=``duplicate``）。

        - 首次 ``command_id``：创建规范行，``outcome`` 取 WP-14C 判定结果，
          ``raw_payload`` 保存首次规范回执原文；
        - 已存在：``duplicate_count += 1``、更新 ``last_duplicate_at`` /
          ``last_payload``，**不覆盖**首次规范回执（``raw_payload`` 不动）；
        - 并发唯一冲突（两请求同时首次到达）：``IntegrityError`` 转为
          duplicate 重试语义 —— 回滚后重查、命中既有行则转累计，
          **不会产生第二条规范行**。

        ★ 调用方（``app.mqtt.handlers.handle_robot_ack``）必须与任务状态
        推进共用一个会话：先本方法写 ACK 行、再状态迁移、最后 commit ——
        写库异常整体回滚，不留下「已推进但无回执证据」的状态。
        """
        wall = received_wall_at or datetime.now(timezone.utc)
        existing = await self.get_by_command_id(command_id)
        if existing is not None:
            return await self._bump_duplicate(existing, raw_payload, wall)

        row = TaskAck(
            command_id=command_id,
            task_id=task_id,
            device_id=device_id,
            seq=seq,
            outcome=outcome,
            accepted=accepted,
            reason=reason or None,
            mode=mode or None,
            received_at=received_at,
            received_wall_at=wall,
            raw_payload=dict(raw_payload),
        )
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError:
            # 并发首次插入撞 command_id 唯一键：本次 flush（连同同事务内
            # 尚未提交的状态改动）整体回滚，然后按 duplicate 语义重查累计。
            # 若回滚后仍查不到（非唯一冲突，如外键缺失），原样上抛，
            # 由 handler 隔离为 warning —— 绝不静默吞掉。
            await self.session.rollback()
            existing = await self.get_by_command_id(command_id)
            if existing is None:
                raise
            return await self._bump_duplicate(existing, raw_payload, wall)
        return outcome

    async def _bump_duplicate(
        self,
        existing: TaskAck,
        raw_payload: dict[str, Any],
        wall: datetime,
    ) -> str:
        """累计重复次数并更新最近一次重复回执，不改写首次规范回执。"""
        existing.duplicate_count += 1
        existing.last_duplicate_at = wall
        existing.last_payload = dict(raw_payload)
        return AckResult.DUPLICATE

    async def list_by_task(
        self,
        task_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[TaskAck], int]:
        """分页查询某任务的 ACK 审计行，默认按平台落库时间倒序（冻结索引）。"""
        conditions = [TaskAck.task_id == task_id]
        total = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(TaskAck)
                    .where(and_(*conditions))
                )
            ).scalar_one()
        )
        stmt = (
            select(TaskAck)
            .where(and_(*conditions))
            .order_by(TaskAck.received_wall_at.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        rows = await self.session.execute(stmt)
        return list(rows.scalars().all()), total

    async def rebuild_ack_tracker(self, tracker: Any) -> int:
        """从 t_task_ack 重建 AckTracker 的规范回执与设备 ACK 水位（WP-14D §5）。

        返回重建的行数。重建内容：
        - 规范回执：``command_id → AckRecord``（供 ``duplicate`` 判定）；
        - 设备 ACK 水位：``device_id → max(seq)``（供 ``out_of_order`` 判定）。

        实现：先用 :meth:`AckTracker.record`（公共 API）重放每行规范回执，
        再按账本逐设备 ``max(seq)`` 校正水位 —— ``record`` 只对 ``new``
        提升水位，``late / out_of_order`` 行的水位需按账本补全，否则
        重启后乱序判定会偏松。校正直接写 ``_max_acked_seq`` 是 WP-14D
        与 WP-14C 内存跟踪器的显式集成点（跟踪器未暴露水位 setter，
        ack.py 属 WP-14C 产物不在本包改动范围）。

        ★ 恢复边界（如实声明，不伪装）：
        - ``duplicate`` / ``out_of_order`` 判定重启后**可恢复**；
        - ``late`` 判定依赖发布时登记的 ``expires_at``（t_task_ack 不存），
          已 ACK 的命令已在 ``_acks`` 内不受影响；「发布后未 ACK」的命令
          重启后仍无法判 late —— 属 WP-14C 内存登记表的固有边界；
        - 即使不重建：账本 + 任务状态守卫仍保证重复回执不重复推进
          （record_arrival 以账本为准返回 duplicate）。
        """
        rows = list(
            (await self.session.execute(select(TaskAck))).scalars().all()
        )
        for row in rows:
            tracker.record(
                AckEnvelope(
                    command_id=row.command_id,
                    task_id=row.task_id,
                    device_id=row.device_id,
                    seq=row.seq,
                    received_at=_as_utc(row.received_at).timestamp(),
                    accepted=row.accepted,
                    reason=row.reason or "",
                    mode=row.mode or "",
                ),
                outcome=row.outcome,
            )
        # 水位校正：按账本逐设备 max(seq)（见 docstring）。
        watermark: dict[str, int] = {}
        for row in rows:
            cur = watermark.get(row.device_id, -1)
            if row.seq > cur:
                watermark[row.device_id] = row.seq
        for device_id, max_seq in watermark.items():
            with tracker._lock:
                tracker._max_acked_seq[device_id] = max(
                    tracker._max_acked_seq.get(device_id, -1), max_seq
                )
        return len(rows)
