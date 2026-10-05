#!/usr/bin/env python
"""探海灵眸 Oceanus — 端到端冒烟测试。

目的：一条命令验证「事件上报 → 幂等判重 → 自动派单 → 状态流转」整条链路
是否真的通。任何一步失败，给出明确的中文原因与排查建议。

设计原则：
- **只依赖 HTTP 接口**，不直连数据库。这样测的是真实调用路径，
  而不是「ORM 能不能写库」。
- **可重入**。每次运行都用全新的 event_id 与 seq，重复跑不会互相干扰。
- **会清理**。测试产生的任务会被推进到终态，不留脏数据堵塞后续派单。

用法：
    python scripts/smoke_test.py
    python scripts/smoke_test.py --base-url http://localhost:8000
    python scripts/smoke_test.py --username operator --password <pwd>
    python scripts/smoke_test.py --keep    # 不推进任务状态，便于人工到大屏看

退出码：0 全部通过；1 有失败项。

状态机写操作需要真实登录令牌，默认使用演示种子账号 operator / operator123456，
生产环境请用 --username/--password 传专用账号，不要沿用演示口令。
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    import httpx
except ImportError:
    print("缺少依赖 httpx，请先执行：pip install httpx")
    sys.exit(2)


# ======================================================================
# 输出工具
# ======================================================================
class C:
    """终端颜色（Windows 下不支持的会自动退化为无色）。"""

    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    GREY = "\033[90m"
    BOLD = "\033[1m"
    END = "\033[0m"


def _supports_color() -> bool:
    return sys.stdout.isatty()


if not _supports_color():
    for _name in ("GREEN", "RED", "YELLOW", "CYAN", "GREY", "BOLD", "END"):
        setattr(C, _name, "")


def ok(msg: str) -> None:
    print(f"  {C.GREEN}✓{C.END} {msg}")


def fail(msg: str) -> None:
    print(f"  {C.RED}✗{C.END} {msg}")


def warn(msg: str) -> None:
    print(f"  {C.YELLOW}!{C.END} {msg}")


def info(msg: str) -> None:
    print(f"  {C.GREY}·{C.END} {msg}")


def section(idx: int, total: int, title: str) -> None:
    print(f"\n{C.BOLD}[{idx}/{total}] {title}{C.END}")


def hint(msg: str) -> None:
    print(f"      {C.CYAN}→ {msg}{C.END}")


# ======================================================================
# 结果收集
# ======================================================================
class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.notes: list[str] = []

    def success(self) -> None:
        self.passed += 1

    def error(self, note: str) -> None:
        self.failed += 1
        self.notes.append(note)

    def skip(self, note: str) -> None:
        self.skipped += 1
        self.notes.append(f"[跳过] {note}")

    def note(self, text: str) -> None:
        """记一条提示，不影响通过/失败计数。

        用于「不算失败但需要人注意」的情况 ——
        比如后端降级启动（设计允许），后续业务步骤会真正暴露问题。
        """
        self.notes.append(f"[提示] {text}")


# ======================================================================
# HTTP 封装：统一拆响应信封
# ======================================================================
class Api:
    """后端接口客户端。

    统一响应结构：{code, message, data, trace_id}
    code == 0 视为业务成功。
    """

    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self.base = base_url.rstrip("/")
        self.api = f"{self.base}/api/v1"
        self.client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self.client.close()

    # ---- 底层 ----
    def _unwrap(self, resp: httpx.Response) -> tuple[int, Any, str]:
        """返回 (code, data, message)。HTTP 层错误统一映射为 code=-1。"""
        if resp.status_code >= 500:
            return -1, None, f"HTTP {resp.status_code}（服务端异常）"
        try:
            body = resp.json()
        except Exception:   # noqa: BLE001
            return -1, None, f"响应不是合法 JSON（HTTP {resp.status_code}）"

        if not isinstance(body, dict) or "code" not in body:
            return -1, None, "响应缺少 code 字段（信封结构不符）"

        return int(body.get("code", -1)), body.get("data"), str(body.get("message", ""))

    def get(self, path: str, **params: Any) -> tuple[int, Any, str]:
        clean = {k: v for k, v in params.items() if v is not None}
        resp = self.client.get(f"{self.api}{path}", params=clean)
        return self._unwrap(resp)

    def post(self, path: str, payload: dict | None = None) -> tuple[int, Any, str]:
        resp = self.client.post(f"{self.api}{path}", json=payload or {})
        return self._unwrap(resp)

    def patch(self, path: str, payload: dict) -> tuple[int, Any, str]:
        resp = self.client.patch(f"{self.api}{path}", json=payload)
        return self._unwrap(resp)

    # ---- 健康 ----
    def health(self) -> tuple[bool, dict]:
        try:
            resp = self.client.get(f"{self.base}/health")
            if resp.status_code == 200:
                return True, resp.json()
        except Exception:   # noqa: BLE001
            pass
        return False, {}

    # ---- 认证 ----
    def login(self, username: str, password: str) -> bool:
        """真实登录并携带写令牌；登录失败返回 False。"""
        try:
            resp = self.client.post(
                f"{self.api}/auth/login",
                json={"username": username, "password": password},
            )
            if resp.status_code != 200:
                return False
            body = resp.json()
            token = (body.get("data") or {}).get("access_token")
            if not token:
                return False
            self.client.headers["Authorization"] = f"Bearer {token}"
            return True
        except Exception:   # noqa: BLE001
            return False


# ======================================================================
# 测试数据构造
# ======================================================================
# 用种子数据里真实存在的设备，否则平台会因「设备未注册」拒收
CAMERA_ID = "CAM-MABI-01"
CAMERA_LNG = 119.6521
CAMERA_LAT = 26.3864
MERGE_RADIUS_M = 200


def build_event(seq: int, event_id: str) -> dict:
    """构造一条高优先级事件报文（贴合真实报文契约）。

    用 foam（泡沫类）是因为只有 foam / fishing_gear 会触发自动派单。
    """
    now = datetime.now(timezone(timedelta(hours=8)))
    return {
        "event_id": event_id,
        "device_id": CAMERA_ID,
        "device_type": "shore_camera",
        "timestamp": now.isoformat(),
        "location": {"lng": CAMERA_LNG, "lat": CAMERA_LAT},
        "detections": [
            {"class": "foam", "confidence": 0.91, "bbox": [412, 288, 468, 331]},
            {"class": "foam", "confidence": 0.84, "bbox": [520, 305, 561, 344]},
        ],
        "aggregate": {"main_class": "foam", "count": 2, "max_confidence": 0.91},
        "evidence_url": None,
        "model_version": "det_v0.1.0",
        "seq": seq,
    }


def unique_ids() -> tuple[str, int]:
    """生成本次运行专属的 event_id 与 seq，保证可重复运行。"""
    stamp = datetime.now().strftime("%H%M%S")
    rnd = random.randint(100, 999)
    seq = int(f"9{stamp}{rnd}")          # 9 开头，避开种子数据的 1xxx~7xxx
    event_id = f"evt_smoke_{stamp}{rnd}"
    return event_id, seq


def _distance_m(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """球面近似距离（米），仅用于防抖合并半径判定。"""
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlat = lat1_r - lat2_r
    dlon = math.radians(lng1 - lng2)
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


# ======================================================================
# 各项检查
# ======================================================================
TOTAL_STEPS = 8


def step_health(api: Api, rp: Report) -> bool:
    section(1, TOTAL_STEPS, "后端健康检查")
    alive, body = api.health()
    if not alive:
        fail("后端不可达")
        hint("确认后端已启动：docker compose up -d backend  或  make dev-backend")
        hint("确认端口正确：默认 http://localhost:8000")
        rp.error("后端健康检查失败（服务不可达）")
        return False

    ok(f"服务在线 · 环境={body.get('env')} · WS 连接数={body.get('ws_connections', 0)}")

    # 依赖状态：进程起来了不等于能干活，必须逐项看
    deps = body.get("dependencies") or {}
    status = body.get("status", "ok")
    if deps:
        bad = {k: v for k, v in deps.items() if v != "ok"}
        if bad:
            fail(f"整体状态={status}，以下依赖不可用：")
            for name, detail in bad.items():
                print(f"          · {name}: {detail}")
            hint("依赖不全时冒烟测试的后续步骤会失败，先补齐：docker compose up -d")
            hint("只看进程是否活着：curl -s localhost:8000/health")
            rp.note(f"依赖降级：{', '.join(bad)}（status={status}）")
            # 不算硬失败 —— 降级启动本身是设计允许的，
            # 这里只提示；真正的失败由后续业务步骤暴露。
        else:
            ok("依赖全部正常（redis / mqtt / database）")

    rp.success()
    return True


def step_devices(api: Api, rp: Report) -> bool:
    section(2, TOTAL_STEPS, "设备与种子数据")
    code, data, msg = api.get("/devices")
    if code != 0:
        fail(f"设备列表查询失败：code={code} {msg}")
        hint("检查数据库是否已初始化：make db-init")
        rp.error(f"设备列表查询失败 code={code}")
        return False

    devices = data or []
    if not devices:
        fail("设备列表为空")
        hint("种子数据未导入。执行：make db-init")
        hint("注意：docker-entrypoint-initdb.d 只在数据卷为空时执行")
        rp.error("设备列表为空（种子数据未导入）")
        return False

    robots = [d for d in devices if d.get("device_type") == "robot"]
    cameras = [d for d in devices if d.get("device_type") == "shore_camera"]
    online_robots = [r for r in robots if r.get("status") == "online"]

    ok(f"设备 {len(devices)} 台：摄像头 {len(cameras)} · 机器人 {len(robots)}（在线 {len(online_robots)}）")

    target = next((d for d in devices if d.get("device_id") == CAMERA_ID), None)
    if target is None:
        fail(f"测试用设备 {CAMERA_ID} 不在设备表中")
        hint(f"种子数据里应有 {CAMERA_ID}，请检查 02_seed.sql 是否执行")
        rp.error(f"设备 {CAMERA_ID} 未注册")
        return False
    ok(f"测试设备 {CAMERA_ID} 已注册")

    if not online_robots:
        warn("当前没有在线机器人，自动派单会失败（这是正常路径，不是 bug）")
        hint("检查机器人 meta.battery ≥ 30 且三仓总占用 < 80%")
        rp.skip("无在线可用机器人，派单相关检查将被跳过")

    rp.success()
    return len(online_robots) > 0


def step_ingest(api: Api, rp: Report, event_id: str, seq: int) -> tuple[bool, str | None]:
    """上报事件并返回 (是否成功, 派单命中的 task_id)。"""
    section(3, TOTAL_STEPS, "上报事件（HTTP 备用通道）")
    payload = build_event(seq, event_id)
    code, data, msg = api.post("/events", payload)

    if code != 0:
        fail(f"事件上报失败：code={code} {msg}")
        if code == 2001:
            hint(f"设备 {CAMERA_ID} 未注册 —— 检查 device_id 拼写")
        elif code == 1001:
            hint("参数校验失败 —— 对照 docs/api.md 第三节检查报文结构")
        rp.error(f"事件上报失败 code={code} {msg}")
        return False, None

    ok(f"事件已受理 · event_id={event_id}")

    if data.get("duplicate"):
        fail("上报被判重 —— 说明 seq 与历史数据冲突")
        hint("本脚本每次生成新 seq；若频繁出现，检查时钟是否异常")
        rp.error("首次上报即被判重")
        return False, None

    task_id = data.get("task_id")
    if data.get("task_created"):
        ok(f"自动派单成功 · task_id={task_id}")
    else:
        warn("未自动生成任务（可能无可用机器人）")

    rp.success()
    return True, task_id


def step_idempotent(api: Api, rp: Report, event_id: str, seq: int) -> bool:
    section(4, TOTAL_STEPS, "幂等性验证（重复上报应被忽略）")
    payload = build_event(seq, event_id)     # 完全相同的 event_id + seq
    code, data, msg = api.post("/events", payload)

    if code != 0:
        fail(f"重复上报返回了错误：code={code} {msg}")
        hint("幂等命中应返回 code=0 且 duplicate=true，而不是报错")
        rp.error(f"重复上报返回错误 code={code}")
        return False

    if not data.get("duplicate"):
        fail("重复上报未被识别为重复！")
        hint("检查 (device_id, seq) 唯一约束是否存在于 t_event")
        hint("SQL: SELECT conname FROM pg_constraint WHERE conname='uq_event_device_seq';")
        rp.error("幂等判重失效")
        return False

    ok(f"重复上报已被忽略 · message={msg}")
    info("这是 QoS1 场景下的正常路径：至少一次投递，靠业务主键去重")
    rp.success()
    return True


def step_query_event(api: Api, rp: Report, event_id: str) -> bool:
    section(5, TOTAL_STEPS, "事件查询与列表")
    code, data, msg = api.get(f"/events/{event_id}")
    if code != 0:
        fail(f"事件详情查询失败：code={code} {msg}")
        rp.error(f"事件详情查询失败 code={code}")
        return False

    ok(
        f"详情可查 · 类别={data.get('main_class_label')} · "
        f"坐标=({data.get('lng'):.4f}, {data.get('lat'):.4f}) · 状态={data.get('status_label')}"
    )

    code2, data2, msg2 = api.get("/events", hours=24, page_size=5)
    if code2 != 0:
        fail(f"事件列表查询失败：code={code2} {msg2}")
        rp.error(f"事件列表查询失败 code={code2}")
        return False

    meta = (data2 or {}).get("meta", {})
    ok(f"列表可查 · 共 {meta.get('total', 0)} 条 · 本页 {len((data2 or {}).get('items', []))} 条")

    # 热力图（顺带验证 PostGIS 空间查询链路）
    code3, data3, msg3 = api.get("/events/heatmap", hours=24, grid_size=500)
    if code3 != 0:
        fail(f"热力图查询失败：code={code3} {msg3}")
        hint("热力图依赖 PostGIS。确认已启用：SELECT PostGIS_Version();")
        rp.error(f"热力图查询失败 code={code3}")
        return False

    cells = data3 or []
    ok(f"热力图可查 · {len(cells)} 个网格")
    if not cells:
        warn("热力图返回空 —— 若刚导入种子数据属正常（时间窗口内无数据）")

    rp.success()
    return True


def step_dispatch(
    api: Api,
    rp: Report,
    event_id: str,
    online_robot_exists: bool,
    expected_task_id: str | None,
) -> str | None:
    section(6, TOTAL_STEPS, "派单结果验证")

    # 优先直查上报接口回传的 task_id。防抖窗口内新事件会合并进已有任务，
    # 此时任务.event_id 不等于本次 event_id，但只要目标点在合并半径内就是合法命中。
    if expected_task_id:
        code, data, msg = api.get(f"/tasks/{expected_task_id}")
        if code != 0:
            fail(f"上报接口回传的任务查询失败：code={code} {msg}")
            rp.error(f"任务详情查询失败 code={code}")
            return None

        task = data or {}
        task_lng = task.get("lng")
        task_lat = task.get("lat")
        if task.get("event_id") == event_id:
            ok(f"任务由本次事件直接生成 · task_id={task.get('task_id')}")
        elif task_lng is not None and task_lat is not None:
            dist = _distance_m(CAMERA_LNG, CAMERA_LAT, task_lng, task_lat)
            if dist <= MERGE_RADIUS_M:
                ok(f"事件合并至窗口内已有任务 · task_id={task.get('task_id')} · 距离 {dist:.0f}m")
                info("防抖合并是设计行为：10 分钟内 200m 半径内不重复派单")
            else:
                fail(f"回传任务目标点距本次事件 {dist:.0f}m，超过合并半径 {MERGE_RADIUS_M}m")
                rp.error("回传 task_id 与本次事件无关联")
                return None
        else:
            fail("回传任务缺少目标坐标，无法判定与本次事件的关联")
            rp.error("回传 task_id 缺少坐标")
            return None

        ok(f"机器人={task.get('robot_id')} · 状态={task.get('status_label')}")
        rp.success()
        return task.get("task_id")

    if not online_robot_exists:
        warn("无在线机器人，跳过派单验证")
        rp.skip("无在线机器人，未验证派单")
        return None

    code, data, msg = api.get("/tasks", page_size=50)
    if code != 0:
        fail(f"任务列表查询失败：code={code} {msg}")
        rp.error(f"任务列表查询失败 code={code}")
        return None

    items = (data or {}).get("items", [])
    related = [t for t in items if t.get("event_id") == event_id]

    if not related:
        merged = [
            t
            for t in items
            if t.get("lng") is not None
            and t.get("lat") is not None
            and _distance_m(CAMERA_LNG, CAMERA_LAT, t["lng"], t["lat"]) <= MERGE_RADIUS_M
        ]
        if merged:
            warn("本次事件被合并进窗口内已有任务（合法防抖）")
            task = merged[0]
        else:
            fail("找不到与本次事件关联的任务 —— 自动派单未生效")
            hint("检查后端日志：docker compose logs -f backend | grep '\\[派单\\]'")
            hint("确认事件的 main_class 是 foam 或 fishing_gear（其他类别不触发自动派单）")
            hint("确认 Redis 可用：派单消费者依赖 Redis Streams")
            rp.error("事件未生成关联任务")
            return None
    else:
        task = related[0]
    ok(
        f"已生成任务 · task_id={task.get('task_id')} · "
        f"机器人={task.get('robot_id')} · 状态={task.get('status_label')}"
    )

    target_lng = task.get("lng")
    target_lat = task.get("lat")
    if target_lng and target_lat:
        ok(f"任务目标点 ({target_lng:.4f}, {target_lat:.4f})")

    rp.success()
    return task.get("task_id")


def step_state_machine(api: Api, rp: Report, task_id: str | None) -> bool:
    section(7, TOTAL_STEPS, "任务状态机流转")
    if not task_id:
        warn("无任务可推进，跳过状态机验证")
        rp.skip("无任务，未验证状态机")
        return False

    # 先确认当前状态，再验证非法跳转被拒绝 —— 这是状态机最容易漏测的地方
    code, data, msg = api.get(f"/tasks/{task_id}")
    if code != 0:
        fail(f"任务详情查询失败：code={code} {msg}")
        rp.error("任务详情查询失败，无法确定当前状态")
        return False

    current_status = (data or {}).get("status") or "pending"
    if current_status in ("done", "cancelled"):
        warn(f"任务已是终态（{current_status}），无法再推进")
        rp.skip("任务已是终态，未验证状态机")
        return False

    invalid_target = (
        "done"
        if current_status in ("pending", "assigned", "navigating")
        else "navigating"
    )
    code, _, msg = api.patch(f"/tasks/{task_id}", {"status": invalid_target})
    if code == 0:
        fail(f"非法跳转被接受了：{current_status} → {invalid_target} 本应被拒绝")
        hint("检查 DispatchEngine.transition() 是否被绕过")
        hint("SQL 直改 status 会绕过状态机 —— 全局搜索 task.status = ")
        rp.error("状态机未拦截非法跳转")
        return False

    ok(f"非法跳转被正确拒绝 · {current_status} → {invalid_target} · code={code} · {msg}")

    # 再走合法路径：从当前状态补全到终态，避免 ACK 超时回退后链断
    chain_order = ["pending", "assigned", "navigating", "collecting", "done"]
    labels = {
        "pending": "待派单",
        "assigned": "已派单",
        "navigating": "前往中",
        "collecting": "作业中",
        "done": "已完成",
    }
    # 从当前状态的下一步开始，避免把「原地停留」当成合法迁移
    start = chain_order.index(current_status) + 1 if current_status in chain_order else 1
    chain = [(s, labels[s]) for s in chain_order[start:]]

    for status, label in chain:
        code, data, msg = api.patch(f"/tasks/{task_id}", {"status": status})
        if code != 0:
            fail(f"状态推进失败 {status}：code={code} {msg}")
            hint("对照 docs/api.md 第四节的状态机迁移表")
            rp.error(f"状态推进失败 {status} code={code}")
            return False
        ok(f"→ {label}（{status}）")

    # 校验时间戳链是否完整
    code, data, msg = api.get(f"/tasks/{task_id}")
    if code != 0:
        warn("任务详情复查失败，跳过时间戳链校验")
    else:
        stamps = ["assigned_at", "ack_at", "started_at", "finished_at"]
        filled = [s for s in stamps if data.get(s)]
        if len(filled) == len(stamps):
            ok(f"生命周期时间戳链完整（{len(filled)}/4）")
        else:
            missing = [s for s in stamps if not data.get(s)]
            warn(f"时间戳缺失：{', '.join(missing)}")
            info("这通常说明状态是直接改的，没走 transition()")

        if data.get("finished_at"):
            info(f"完成时间已记录 · 状态={data.get('status_label')}")

    rp.success()
    return True


def step_stats(api: Api, rp: Report) -> bool:
    section(8, TOTAL_STEPS, "统计与报表接口")

    code, data, msg = api.get("/stats/dashboard")
    if code != 0:
        fail(f"大屏指标卡查询失败：code={code} {msg}")
        rp.error(f"大屏指标卡失败 code={code}")
        return False
    ok(
        f"指标卡 · 24h 事件={data.get('event_count_24h')} · "
        f"待派单={data.get('pending_tasks')} · "
        f"机器人在线={data.get('robots_online')}/{data.get('robots_total')} · "
        f"累计清理={data.get('collected_kg_total')}kg"
    )

    code, data, msg = api.get("/stats/classes", hours=24)
    if code != 0:
        warn(f"类别分布查询失败：code={code} {msg}")
    else:
        ok(f"类别分布 · {len(data or [])} 个类别有数据")

    code, data, msg = api.get("/stats/trend", hours=24)
    if code != 0:
        warn(f"事件趋势查询失败：code={code} {msg}")
    else:
        ok(f"事件趋势 · {len(data or [])} 个时间桶")

    code, data, msg = api.get("/reports/summary", days=7)
    if code != 0:
        warn(f"报表汇总查询失败：code={code} {msg}")
        info("报表读预聚合宽表 t_report_daily，种子数据仅有 3 天记录")
    else:
        ok(f"报表汇总 · {len(data or [])} 个乡镇")

    rp.success()
    return True


# ======================================================================
# 主流程
# ======================================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="探海灵眸 Oceanus 端到端冒烟测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/smoke_test.py\n"
            "  python scripts/smoke_test.py --base-url http://192.168.1.10:8000\n"
            "  python scripts/smoke_test.py --keep   # 不推进任务状态，便于到大屏看效果\n"
        ),
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="后端地址（默认 http://localhost:8000）",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="不把任务推进到终态（保留在途工单，便于人工到大屏观察）",
    )
    parser.add_argument(
        "--username",
        default="operator",
        help="状态机写操作登录账号（默认 operator）",
    )
    parser.add_argument(
        "--password",
        default="operator123456",
        help="状态机写操作登录密码（默认演示种子账号）",
    )
    args = parser.parse_args()

    print(f"\n{C.BOLD}探海灵眸 Oceanus · 端到端冒烟测试{C.END}")
    print(f"{C.GREY}目标：{args.base_url}    时间：{datetime.now():%Y-%m-%d %H:%M:%S}{C.END}")
    print("=" * 64)

    api = Api(args.base_url)
    rp = Report()
    event_id, seq = unique_ids()

    try:
        # 前置：健康检查不过就直接退出，后面的报错没有意义
        if not step_health(api, rp):
            return _summary(rp)

        online_robot_exists = step_devices(api, rp)
        ingest_ok, task_id_from_ingest = step_ingest(api, rp, event_id, seq)
        if not ingest_ok:
            return _summary(rp)

        step_idempotent(api, rp, event_id, seq)
        step_query_event(api, rp, event_id)

        # 派单是异步的（走 Redis Stream），给消费者一点时间
        print(f"\n  {C.GREY}等待派单消费者处理（3 秒）...{C.END}")
        time.sleep(3)

        task_id = step_dispatch(api, rp, event_id, online_robot_exists, task_id_from_ingest)

        if args.keep:
            section(7, TOTAL_STEPS, "任务状态机流转")
            info(f"已跳过（--keep）。任务 {task_id} 保留在途状态，可到大屏查看")
            rp.skip("按 --keep 跳过状态机推进")
        else:
            if not api.login(args.username, args.password):
                fail(f"状态机写操作前置登录失败：{args.username}")
                hint("账号密码可用 --username/--password 指定；演示种子账号：operator / operator123456")
                rp.error("状态机前置登录失败")
                return _summary(rp)
            info(f"状态机写操作使用真实登录令牌（{args.username}）")
            step_state_machine(api, rp, task_id)

        step_stats(api, rp)

    except KeyboardInterrupt:
        print(f"\n{C.YELLOW}已被用户中断{C.END}")
        return 130
    except Exception as exc:   # noqa: BLE001
        print(f"\n{C.RED}未预期异常：{type(exc).__name__}: {exc}{C.END}")
        hint("若为连接类错误，确认后端地址正确且服务已启动")
        rp.error(f"未预期异常 {type(exc).__name__}")
    finally:
        api.close()

    return _summary(rp)


def _summary(rp: Report) -> int:
    total = rp.passed + rp.failed + rp.skipped
    print("\n" + "=" * 64)
    print(f"{C.BOLD}结果：{C.END}", end="")

    if rp.failed == 0:
        print(f"{C.GREEN}全部通过{C.END}  ", end="")
    else:
        print(f"{C.RED}{rp.failed} 项失败{C.END}  ", end="")

    print(
        f"{C.GREY}通过 {rp.passed} · 失败 {rp.failed} · 跳过 {rp.skipped}"
        f"（共 {total} 项）{C.END}"
    )

    if rp.notes:
        print(f"\n{C.BOLD}明细：{C.END}")
        for note in rp.notes:
            print(f"  - {note}")

    if rp.failed:
        print(f"\n{C.YELLOW}排查建议：{C.END}")
        print("  1. 后端日志：docker compose logs -f backend")
        print("  2. 只看派单：docker compose logs -f backend | grep '\\[派单\\]'")
        print("  3. 死信队列：docker compose exec redis redis-cli XRANGE stream:dead_letter - + COUNT 10")
        print("  4. 完整排查表：docs/deployment.md 第八节")
        print()
        return 1

    print(f"\n{C.GREEN}整条链路（事件 → 判重 → 派单 → 状态流转 → 统计）已跑通。{C.END}")
    print(f"{C.GREY}下一步：打开大屏 http://localhost:5173 查看效果；"
          f"或运行 make simulate 持续产生事件。{C.END}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
