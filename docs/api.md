# 接口契约（HTTP API）

> 基础地址：`http://localhost:8000/api/v1`
>
> 在线文档：`http://localhost:8000/docs`（Swagger）/ `http://localhost:8000/redoc`

---

## 一、通用约定

### 1.1 统一响应信封

**所有**业务接口都返回同一结构：

```json
{
  "code": 0,
  "message": "ok",
  "data": { },
  "trace_id": "a3f2c9d1e8b74f02"
}
```

| 字段 | 说明 |
| --- | --- |
| `code` | `0` = 成功；非 0 = 业务错误码（见 1.3） |
| `message` | 面向用户的中文提示，前端可直接展示 |
| `data` | 业务数据，失败时为 `null` |
| `trace_id` | 链路追踪 ID，排查问题时让用户提供此值 |

**为什么 HTTP 状态码统一 200**：前端 axios 拦截器只需看 `code` 一个维度，不用同时处理 HTTP 状态与业务码两套逻辑。只有系统级错误（参数校验 422、未捕获异常 500）才用非 200。

前端 `src/api/http.js` 的响应拦截器已统一拆信封：调用方直接拿到 `data`。

### 1.2 请求头

| 头 | 说明 |
| --- | --- |
| `Authorization` | `Bearer <access_token>`；登录后由前端统一携带 |
| `X-Trace-Id` | 可选，客户端自带追踪 ID；不带则服务端生成 |

身份与角色的唯一真源是服务端签名令牌。后端**不接受** `X-User`、`X-Role`
或 `X-Scope` 请求头；这些值由客户端填写时可以被任意伪造。未登录或令牌无效
时按匿名只读处理，写接口仍会返回无权限。

角色为 `admin`、`operator`、`approver`、`viewer`。其中 `approver` 只可决定
Agent 人工审批，不具备工单创建、状态更新或补派权限。

### 1.3 错误码表

| 段 | 范围 | 含义 |
| --- | --- | --- |
| 1xxx | 1000–1005 | 通用：未知 / 参数非法 / 未找到 / 未授权 / 无权限 / 冲突 |
| 2xxx | 2001–2003 | 设备：不存在 / 离线 / seq 重复 |
| 3xxx | 3001–3002 | 事件：不存在 / 重复 |
| 4xxx | 4001–4004 | 任务：不存在 / 非法状态跳转 / 无可用机器人 / ACK 超时 |
| 5xxx | 5001–5002 | AI：服务不可用 / 推理失败 |
| 6xxx | 6001–6009 | Agent：运行不存在 / 状态非法 / 审批不可决 / 任务冲突等 |
| 7xxx | 7001–7010 | 知识域：资产或版本不存在 / 本体审核非法 / 检索冲突 / 输入非法等 |

具体值见 `backend/app/core/exceptions.py::ErrorCode`。

### 1.4 分页结构

```json
{
  "items": [ ],
  "meta": { "total": 128, "page": 1, "page_size": 20 }
}
```

---

## 二、认证 `/auth`

### POST `/auth/login` — 账号登录

**请求**
```json
{ "username": "admin", "password": "<部署时设置的强密码>" }
```

开发环境的种子账号仅用于本地演示，生产环境使用 `make prod-create-admin`
创建正式管理员，不导入 `02_seed.sql`。

**响应**
```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "access_token": "eyJzdWIiOiJhZG1pbiIsInJvbGUiOiJhZG1pbiIsImV4cCI6MTc...b6a2",
    "token_type": "bearer",
    "expires_in": 86400,
    "role": "admin",
    "full_name": "系统管理员"
  }
}
```

失败返回 `code=1003` + HTTP 401。

### GET `/auth/me` — 当前用户信息

```json
{ "username": "admin", "role": "admin", "township_scope": null, "can_write": true }
```

### GET `/auth/users` — 用户列表（仅 admin）

非 admin 调用返回 `code=1004`。

---

## 三、事件 `/events`

### POST `/events` — 事件上报（HTTP 备用通道）

主通道是 MQTT；此接口用于**调试与冒烟测试**，也作为边缘盒在 MQTT 不可用时的降级路径。

**请求体**
```json
{
  "event_id": "evt_CAM-MABI-01_000123",
  "device_id": "CAM-MABI-01",
  "device_type": "shore_camera",
  "timestamp": "2026-09-18T01:23:45+08:00",
  "location": { "lng": 119.6531, "lat": 26.3867 },
  "detections": [
    { "class": "foam", "confidence": 0.91, "bbox": [412, 288, 468, 331] }
  ],
  "aggregate": { "main_class": "foam", "count": 3, "max_confidence": 0.91 },
  "evidence_url": null,
  "model_version": "det_v0.1.0",
  "seq": 123
}
```

**字段约束**

| 字段 | 约束 |
| --- | --- |
| `event_id` | ≤64 字符，全局唯一 |
| `device_id` | ≤64 字符，**必须已在 `t_device` 注册**，否则返回 `code=2001` |
| `device_type` | `shore_camera` / `drone` / `robot` |
| `aggregate.count` | ≥1 |
| `aggregate.max_confidence` | 0~1 |
| `detections` | 可为空（边缘端已聚合），最多 100 条 |
| `seq` | ≥0，**同设备内单调递增**，用于幂等判重 |

**响应**
```json
{
  "accepted": true,
  "event_id": "evt_CAM-MABI-01_000123",
  "duplicate": false,
  "task_created": true,
  "task_id": "tsk_20260918_a3f2c9",
  "message": "事件已受理"
}
```

`duplicate=true` 表示 `(device_id, seq)` 或 `event_id` 已存在，本次被忽略——这是**正常路径**，不是错误。

高优先级类别（`foam` / `fishing_gear`）会同步触发派单尝试。

### GET `/events` — 事件列表

**查询参数**

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `hours` | 24 | 时间窗口（1~720） |
| `main_class` | — | 类别筛选 |
| `status` | — | `new` / `dispatched` / `resolved` / `ignored` |
| `device_id` | — | 设备筛选 |
| `page` | 1 | 页码 |
| `page_size` | 20 | 每页条数（1~200） |

**响应**（`items[]` 中每项）
```json
{
  "event_id": "evt_demo_0001",
  "device_id": "CAM-MABI-01",
  "event_time": "2026-09-17T23:23:45+08:00",
  "lng": 119.653,
  "lat": 26.387,
  "main_class": "foam",
  "main_class_label": "泡沫类",
  "det_count": 5,
  "max_confidence": 0.91,
  "evidence_url": null,
  "model_version": "det_v0.1.0",
  "status": "new",
  "status_label": "待处理",
  "created_at": "2026-09-17T23:23:45+08:00"
}
```

### GET `/events/heatmap` — 热力图聚合

**查询参数**

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `hours` | 24 | 时间窗口 |
| `grid_size` | 500 | 网格边长（**米**，100~5000） |
| `main_class` | — | 类别筛选 |

**响应**
```json
[
  { "lng": 119.6532, "lat": 26.3868, "count": 12, "density": 0.000048, "main_class": "foam" }
]
```

> **两个必读注意点**
>
> 1. `density` 是**单位面积密度**（个/m²），不是总数。返回总数会让大网格天然更热，热力图失真。
> 2. `grid_size` 单位是**米**。服务端内部会把坐标投影到 EPSG:3857 再聚合，若直接在 4326 上做，1 度 ≈ 100km，网格毫无意义。

### GET `/events/{event_id}` — 事件详情

不存在返回 `code=3001`。

---

## 四、任务 `/tasks`

### POST `/tasks` — 人工创建任务

自动派单走事件流程；本接口用于人工干预（平台判读后手动派单）。

**请求体**
```json
{
  "event_id": "evt_demo_0001",
  "robot_id": "RBT-001",
  "target": { "lng": 119.653, "lat": 26.387 },
  "priority": 3
}
```

`robot_id` 为空时任务停在 `pending` 等派单；指定时直接置为 `assigned`。

### GET `/tasks` — 任务列表

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `status` | — | 状态筛选 |
| `robot_id` | — | 机器人筛选 |
| `page` / `page_size` | 1 / 50 | 分页 |

**响应项**（含完整生命周期时间戳链）
```json
{
  "task_id": "tsk_demo_0001",
  "event_id": "evt_demo_0005",
  "robot_id": "RBT-001",
  "lng": 119.6518,
  "lat": 26.3858,
  "status": "done",
  "status_label": "已完成",
  "priority": 1,
  "created_at": "2026-09-17T13:23:45+08:00",
  "assigned_at": "2026-09-17T13:28:45+08:00",
  "ack_at": "2026-09-17T13:29:45+08:00",
  "started_at": "2026-09-17T13:33:45+08:00",
  "finished_at": "2026-09-17T13:53:45+08:00",
  "collected_weight": 12.5,
  "review_result": "confirmed",
  "remark": null
}
```

### PATCH `/tasks/{task_id}` — 更新任务状态

**请求体**
```json
{
  "status": "collecting",
  "robot_id": "RBT-001",
  "collected_weight": 0,
  "review_result": "pending",
  "remark": "现场有风浪，作业延缓"
}
```

**★ 状态机约束**——非法跳转返回 `code=4002`：

```
pending ──► assigned ──► navigating ──► collecting ──► done
   │            │             │              │
   │            └──► pending  │              │
   │            （ACK 超时回退）              │
   └────────────┴─────────────┴──────────────┴──► cancelled
```

合法迁移表：

| 当前状态 | 允许迁移到 |
| --- | --- |
| `pending` | `assigned`、`cancelled` |
| `assigned` | `navigating`、`pending`（ACK 超时）、`cancelled` |
| `navigating` | `collecting`、`cancelled` |
| `collecting` | `done`、`cancelled` |
| `done` | 终态 |
| `cancelled` | 终态 |

成功更新后，服务端会通过 WebSocket 广播 `task_update`。

### GET `/tasks/{task_id}` — 任务详情

不存在返回 `code=4001`。

### GET `/tasks/{task_id}/acks` — 任务 ACK 审计查询（WP-14D）

按任务分页查询 ACK 审计账本（`t_task_ack`）：解释「平台是否收到过该任务的
回执、何时收到、设备是否接受、为什么拒绝、重复到达几次」。进程重启后仍可
查询 —— 账本由 `backend/app/mqtt/handlers.py::handle_robot_ack` 在状态推进
同一事务内写入。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `page` / `page_size` | 1 / 20 | 分页（page_size ≤ 200） |

**权限**：
- **仅登录用户可读**：匿名（无有效令牌）返回 `401 / code=1003`；
- **operator 只能查询本辖区任务**：按 `township_scope` 过滤，越辖区返回
  `403 / code=1004`；admin 与已登录 viewer 可读全部；
- 任务不存在返回 `404 / code=4001`。

**排序**：默认按 `received_wall_at`（平台首次落库时间）**倒序**。

**响应项**（只返回判定结论与时间线，**不回传** `raw_payload` /
`last_payload` 原始回执原文）：

```json
{
  "command_id": "cmd_tsk_ack_demo_0001",
  "task_id": "tsk_ack_demo_0001",
  "device_id": "RBT-DEMO-01",
  "seq": 1,
  "outcome": "new",
  "accepted": true,
  "reason": "dispatched",
  "mode": "navigating",
  "received_at": "2026-09-19T07:59:30+00:00",
  "received_wall_at": "2026-09-19T08:00:00+00:00",
  "duplicate_count": 0
}
```

`outcome` 取值：`new` / `duplicate` / `late` / `out_of_order`（首次规范回执
的 WP-14C 判定结果）；`duplicate_count` 为后续重复到达次数（QoS1 重投 /
设备重复上报累计，不覆盖首次规范回执）。

### 工单执行仿真 `/simulations` — 可视闭环演练

仿真页从工单进入，复用正式 ACK、遥测、作业回传 handler 与
`DispatchEngine.transition()`；仿真会话只生成设备侧报文，不直接改任务状态。
机器人位置仍写入 `t_track`，WebSocket 仍推送 `robot_status`。

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/simulations/{task_id}` | 读权限 | 当前运行快照；无运行会话时从 `t_track` 回放历史 |
| GET | `/simulations/{task_id}/trajectory` | 读权限 | 已落库的 `t_track` 轨迹点 |
| POST | `/simulations/{task_id}/start` | operator/admin | 启动或继续已有运行会话 |
| POST | `/simulations/{task_id}/pause` | operator/admin | 暂停 |
| POST | `/simulations/{task_id}/resume` | operator/admin | 继续 |
| POST | `/simulations/{task_id}/step` | operator/admin | 暂停态单步执行 |
| POST | `/simulations/{task_id}/speed` | operator/admin | 调整倍速 |
| POST | `/simulations/{task_id}/stop` | operator/admin | 停止本次会话 |

**访问边界**：

- 读取允许匿名只读账号；写操作需要 `operator` 或 `admin`。
- `operator` 只能访问本辖区任务，越辖区返回 `403 / code=1004`。
- 工单不存在返回 `404 / code=4001`。
- 所有控制接口在无实际运行会话时返回未找到；已完成或已取消工单不能重新启动。
- `start` 只接受已绑定机器人的 `assigned`、`navigating`、`collecting` 工单。

**启动 / 倍速请求**：

```json
{ "speed": 2 }
```

`speed` 仅允许 `1`、`2`、`4`；非法值返回 `422 / code=1001`。

**停止请求**：

```json
{ "reason": "操作员手动停止仿真" }
```

**运行快照**：

```json
{
  "task_id": "tsk_demo_0001",
  "event_id": "evt_demo_0005",
  "robot_id": "RBT-001",
  "run_id": "a3f2c9d1e8b7",
  "command_id": "cmd_tsk_demo_0001:sim_a3f2c9d1e8b7",
  "state": "running",
  "phase": "navigating",
  "speed": 2,
  "position": { "lng": 119.6541, "lat": 26.3869 },
  "heading": 84.2,
  "home": { "lng": 119.6518, "lat": 26.3858 },
  "target": { "lng": 119.6531, "lat": 26.3867 },
  "battery": 87,
  "bins": { "foam": 0.42, "plastic": 0.18, "mixed": 0.09 },
  "progress": 0.46,
  "route": [{ "lng": 119.6518, "lat": 26.3858 }],
  "remaining": [{ "lng": 119.6531, "lat": 26.3867 }],
  "started_at": "2026-09-22T08:00:00+00:00",
  "updated_at": "2026-09-22T08:00:12+00:00",
  "finished_at": null,
  "error": null,
  "logs": [
    {
      "seq": 2,
      "ts": "2026-09-22T08:00:01+00:00",
      "kind": "telemetry",
      "message": "navigating 遥测上报",
      "payload": {
        "device_id": "RBT-001",
        "battery": 87,
        "heading": 84.2,
        "speed": 1.2
      }
    }
  ]
}
```

`state` 取值：`running` / `paused` / `done` / `stopped` / `error` /
`history`；`phase` 取值：`ack` / `navigating` / `collecting` / `returning` /
`done`。仿真结束后如果进程内会话仍保留，接口返回终态快照；进程重启或页面
刷新后，如果 `t_track` 有数据，则返回 `state="history"` 的只读历史快照
（`run_id` / `command_id` 为 `null`，`logs` 为空）。两者都没有时 `data`
为 `null`。

**轨迹响应项**：

```json
{
  "seq": 12,
  "lng": 119.6541,
  "lat": 26.3869,
  "ts": "2026-09-22T08:00:12+00:00",
  "battery": 87,
  "bins": { "foam": 0.42, "plastic": 0.18, "mixed": 0.09 },
  "speed": 1.2
}
```

轨迹来自现有 `t_track`，没有新增轨迹表。仿真 ACK 的受控扩展、遥测
`heading` 字段和状态机边界见 `docs/mqtt-topics.md` §5.2、§5.5。

### POST `/tasks/dispatch/pending` — 补派待处理事件

**场景**：机器人离线期间产生的事件积压；机器人重新上线后调用本接口补派。

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `limit` | 10 | 单次最多补派数（1~100） |

**响应**
```json
{ "dispatched": 3, "task_ids": ["tsk_...", "tsk_...", "tsk_..."] }
```

---

## 五、设备 `/devices`

### GET `/devices` — 设备列表

| 参数 | 说明 |
| --- | --- |
| `device_type` | `shore_camera` / `drone` / `robot` |
| `status` | `online` / `offline` / `fault` |

**响应项**
```json
{
  "device_id": "CAM-MABI-01",
  "device_type": "shore_camera",
  "name": "马鼻码头摄像头",
  "lng": 119.6521,
  "lat": 26.3864,
  "status": "online",
  "last_heartbeat": "2026-09-18T00:05:00+08:00",
  "stream_url": "/stream/api/stream.flv?src=CAM-MABI-01",
  "meta": { "model": "HK-DS2CD", "resolution": "1920x1080" }
}
```

### GET `/devices/{device_id}` — 设备详情

### GET `/devices/{device_id}/stream` — 获取视频流地址

**响应**
```json
{
  "device_id": "CAM-MABI-01",
  "flv_url": "/stream/api/stream.flv?src=CAM-MABI-01",
  "webrtc_url": "/stream/api/webrtc?src=CAM-MABI-01",
  "hls_url": "/stream/api/stream.m3u8?src=CAM-MABI-01"
}
```

默认返回同源相对路径，由前端容器的 Nginx `/stream/` 反向代理到 go2rtc；
浏览器不会拿到容器内地址 `go2rtc:1984`。只有显式配置
`STREAM_PUBLIC_BASE_URL` 时才返回外部绝对地址。

| 协议 | 延迟 | 适用 |
| --- | --- | --- |
| WebRTC | <500ms | 需要实时操控时 |
| HTTP-FLV | 1~3s | **大屏默认**（前端用 mpegts.js 播放） |
| HLS | 5~15s | 兼容性兜底（iOS Safari） |

---

## 六、机器人 `/robots`

### GET `/robots` — 机器人列表与实时状态

**响应项**
```json
{
  "robot_id": "RBT-001",
  "name": "打捞机器人 01 号",
  "lng": 119.654,
  "lat": 26.387,
  "status": "online",
  "battery": 92,
  "bins": { "foam": 0.05, "plastic": 0.02, "mixed": 0.01 },
  "current_task_id": "tsk_20260918_a3f2c9",
  "last_heartbeat": "2026-09-18T00:05:00+08:00"
}
```

> `bins` 是「打捞即粗分三仓」设计的软件侧落点。没有仓容数据，三仓在平台上就是空的——所以遥测链路必须把三仓占用带上（见 `mqtt-topics.md`）。

### GET `/robots/{robot_id}` — 机器人详情

---

## 七、统计 `/stats`（大屏实时）

> 与 `/reports` 分开的原因：stats 服务「大屏实时」，查明细、刷新快；reports 服务「治理报表」，读预聚合宽表、刷新慢。数据源与访问频率完全不同。

### GET `/stats/dashboard` — 大屏顶部指标卡

```json
{
  "event_count_24h": 47,
  "pending_tasks": 2,
  "collecting_tasks": 3,
  "done_tasks_24h": 8,
  "robots_online": 2,
  "robots_total": 3,
  "collected_kg_total": 12.5,
  "devices_online": 7,
  "devices_total": 10
}
```

### GET `/stats/classes` — 类别分布

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `hours` | 24 | 时间窗口 |

```json
[
  { "main_class": "foam", "label": "泡沫类", "count": 28 },
  { "main_class": "fishing_gear", "label": "渔具类", "count": 11 }
]
```

饼图数据源。

### GET `/stats/trend` — 事件趋势（按小时）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `hours` | 24 | 时间窗口（1~168） |

```json
[ { "time": "2026-09-17T20:00:00+08:00", "count": 5 } ]
```

折线图数据源。

---

## 八、报表 `/reports`

> 数据源为预聚合宽表 `t_report_daily`——由定时任务每日凌晨生成。报表页只读宽表，即使事件量到百万级仍毫秒响应。
>
> 数据完整性（WP-07）：`coverage_area` 可空 —— `null` = 未统计（`not_available`），
> 不是 0；每个指标携带 `coverage_availability` 三态（`available` / `partial` / `not_available`）。
> 真实 0（有记录但合计为 0）与未统计（null）在结构上不同。

### GET `/reports/daily` — 日报表查询

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `days` | 7 | 回溯天数（1~90） |
| `township` | — | 乡镇筛选 |

```json
[
  {
    "stat_date": "2026-09-17",
    "township": "马鼻镇",
    "main_class": "foam",
    "main_class_label": "泡沫类",
    "event_count": 12,
    "task_count": 3,
    "done_count": 3,
    "collected_kg": 28.5,
    "coverage_area": null,
    "coverage_availability": "not_available"
  }
]
```

### GET `/reports/summary` — 报表汇总（按乡镇聚合）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `days` | 7 | 回溯天数 |

```json
[
  { "township": "马鼻镇", "event_count": 26, "done_count": 6, "collected_kg": 62.0, "coverage_area": null, "coverage_availability": "not_available" }
]
```

---

## 九、知识资产 `/knowledge`

知识域提供“资产登记 → 本体半自动抽取 → 人工审核发布 → 多跳检索 →
决策证据固化”的完整链路。读取接口对匿名只读账号开放；写入需要
`operator`，本体审核与发布需要 `admin` 或 `approver`。`operator` 只能访问
自己辖区内的资产。

### 9.1 接口总览

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| POST | `/knowledge/assets` | operator | 登记文档、表格、图片、事件、遥测或数据集资产 |
| GET | `/knowledge/assets` | 读权限 | 资产列表，支持类型、状态、地区和文本筛选 |
| GET | `/knowledge/assets/{asset_id}` | 读权限 | 资产详情与全部不可变版本 |
| POST | `/knowledge/assets/{asset_id}/versions` | operator | 追加内容版本，不覆盖历史 |
| POST | `/knowledge/ontology/versions` | operator | 创建本体版本 |
| GET | `/knowledge/ontology/versions` | 读权限 | 本体版本列表 |
| GET | `/knowledge/ontology/versions/{version_id}` | 读权限 | 本体版本详情 |
| GET | `/knowledge/ontology/versions/{version_id}/nodes` | 读权限 | 本体节点列表 |
| GET | `/knowledge/ontology/versions/{version_id}/relations` | 读权限 | 本体关系列表 |
| POST | `/knowledge/ontology/extract` | operator | 从指定资产版本抽取本体候选 |
| POST | `/knowledge/ontology/nodes/{node_id}/review` | admin/approver | 审核节点 |
| POST | `/knowledge/ontology/relations/{relation_id}/review` | admin/approver | 审核关系 |
| POST | `/knowledge/ontology/versions/{version_id}/publish` | admin/approver | 发布已审核版本 |
| POST | `/knowledge/search` | 读权限 | 跨文档多跳检索 |
| POST | `/knowledge/decisions` | operator | 创建带证据链的决策轨迹 |
| GET | `/knowledge/decisions` | 读权限 | 决策轨迹分页列表 |
| GET | `/knowledge/decisions/{trace_id}/evidence` | 读权限 | 按顺序读取决策证据 |

### 9.2 POST `/knowledge/assets` — 登记资产

```json
{
  "asset_type": "document",
  "title": "连江县海漂垃圾治理规范",
  "source_uri": "standard://local/marine-waste-2026",
  "source_system": "policy-library",
  "mime_type": "text/markdown",
  "region": "连江县",
  "township": "马鼻镇",
  "security_level": "internal",
  "standard_codes": ["DB35/T-EXAMPLE"],
  "tags": ["海漂垃圾", "治理规范"],
  "initial_content": {
    "content_text": "治理范围、巡查频次和责任分工……",
    "extraction_method": "manual",
    "language": "zh-CN"
  }
}
```

首次发布后，资产身份保持稳定；每次内容变更通过 versions 接口追加
`version_no`，并记录内容哈希。这样检索结果和决策证据可以固定到具体版本。

### 9.3 POST `/knowledge/ontology/extract` — 抽取本体候选

```json
{
  "ontology_version_id": "ontv_20260919_01",
  "asset_version_ids": ["kav_20260919_01"],
  "max_nodes": 20,
  "max_relations": 40,
  "min_term_length": 2
}
```

当前实现使用确定性候选抽取，不宣称已经训练领域大模型或自动保证本体正确。
候选必须经过人工审核；只有审核通过的节点和关系才能随版本发布。

### 9.4 POST `/knowledge/search` — 多跳检索

```json
{
  "query": "马鼻镇泡沫浮球处置依据和责任分工",
  "ontology_version_id": "ontv_20260919_01",
  "hop_depth": 2,
  "asset_types": ["document", "table"],
  "standard_codes": ["DB35/T-EXAMPLE"],
  "limit": 10
}
```

```json
{
  "query": "马鼻镇泡沫浮球处置依据和责任分工",
  "mode": "ontology_multihop",
  "ontology_version_id": "ontv_20260919_01",
  "total": 1,
  "results": [
    {
      "asset_id": "ka_...",
      "asset_version_id": "kav_...",
      "title": "连江县海漂垃圾治理规范",
      "asset_type": "document",
      "score": 0.86,
      "snippet": "……",
      "matched_node_ids": ["on_..."],
      "matched_relation_ids": ["or_..."],
      "hop_count": 2,
      "path": [
        {
          "hop_no": 1,
          "source_node_id": "on_a",
          "relation_id": "or_ab",
          "target_node_id": "on_b",
          "relation_type": "responsibility_of"
        }
      ],
      "citations": ["ka_...#kav_..."]
    }
  ]
}
```

没有可用已发布本体时会回退到资产文本检索，响应中的 `mode` 会如实说明，
不会把普通关键词命中伪装成图谱多跳结果。

### 9.5 POST `/knowledge/decisions` — 固化决策证据

```json
{
  "question": "该事件应依据哪条治理规范派发？",
  "answer_summary": "建议派发到马鼻镇责任工单。",
  "run_id": "run_...",
  "ontology_version_id": "ontv_20260919_01",
  "policy_version": "2026.09",
  "hop_depth": 2,
  "evidence": [
    {
      "asset_id": "ka_...",
      "asset_version_id": "kav_...",
      "node_id": "on_...",
      "relation_id": "or_...",
      "hop_no": 2,
      "citation_text": "马鼻镇负责……",
      "source_uri": "standard://local/marine-waste-2026",
      "score": 0.86
    }
  ]
}
```

证据不足时轨迹仍会保存，状态不会伪装成已形成结论。完整证据通过
`GET /knowledge/decisions/{trace_id}/evidence` 读取，可用于页面回放和审计。

---

## 十、WebSocket `/api/v1/ws/alerts`

> 路径：`ws://localhost:8000/api/v1/ws/alerts?token=<access_token>`。
> 浏览器 WebSocket 不能自定义 `Authorization` 请求头，因此令牌通过 query
> 参数传递。生产环境缺失或令牌无效时，服务端以 WebSocket close code `4401`
> 拒绝连接；开发环境保留匿名连接便于本地验收。

### 服务端推送消息

统一结构：
```json
{ "type": "new_event", "data": { }, "ts": "2026-09-18T00:05:00.123456" }
```

| `type` | 触发时机 | `data` 主要字段 |
| --- | --- | --- |
| `connected` | 连接建立后立即发送 | `{ message }` |
| `new_event` | 新事件入库 | `event_id`、`device_id`、`main_class`、`lng`、`lat`、`det_count`、`confidence`、`event_time` |
| `task_update` | 任务状态变更 | `task_id`、`event_id`、`robot_id`、`status` |
| `robot_status` | 机器人遥测上报 | `robot_id`、`battery`、`status`、`lng`、`lat`、`task_id`、`bins`、`heading`、`speed` |
| `pong` | 收到客户端 ping | — |
| `heartbeat` | 服务端 60 秒无消息时主动探测 | — |

### 客户端消息

```json
{ "type": "ping" }
```

前端 `src/utils/realtime.js` 每 25 秒发一次心跳，采用指数退避重连（1s → 30s 封顶）。

### 兜底策略

WebSocket 只推**增量**。若前端丢消息，数据会永久缺失且不自愈。因此 `App.vue` 保留了 30 秒的全量轮询对账——**WebSocket 负责快，轮询负责对**。

---

## 十一、系统接口

| 接口 | 说明 |
| --- | --- |
| `GET /health` | 存活与降级观测；始终返回 HTTP 200，含 `status`、依赖状态和 `ack_recovery` |
| `GET /ready` | 就绪检查；任一依赖不可用时返回 HTTP 503 |
| `GET /` | 服务信息与接口文档地址 |
| `GET /docs` | Swagger UI |
| `GET /openapi.json` | OpenAPI 规范 |

### 11.1 `/health` 必须反映真实降级

`status` 是三档枚举，**不是**永远返回 `ok`：

| status | 触发条件 |
| --- | --- |
| `ok` | `dependencies` 里三项全为 `ok` |
| `degraded` | 部分依赖不可用 |
| `down` | 所有依赖都不可用 |

```json
{
  "status": "degraded",
  "app": "Oceanus",
  "env": "production",
  "ws_connections": 3,
  "dependencies": {
    "redis": "ok",
    "mqtt": "down: MqttError",
    "database": "ok"
  },
  "ack_recovery": {
    "status": "ok",
    "rows": 12,
    "elapsed_ms": 3.2,
    "detail": null
  }
}
```

`dependencies` 的值只有两种形态：成功为 `"ok"`，失败为 `"down: <异常类名>"`。
刻意不把原始异常消息塞进去 —— 异常文本可能带连接串、主机名、账号，
而 `/health` 通常是**免认证**暴露给编排系统的。

> 每个依赖的探测都有 2 秒超时，所以即使数据库在黑洞路由上，
> `/health` 也能在 6 秒内返回，不会把探针挂死。

`ack_recovery` 是应用启动时从 `t_task_ack` 恢复 ACK 判重水位的观测结果；
恢复失败会降级为 `degraded`，但不会阻止进程启动。编排系统应以 `/ready`
决定是否接入流量：三项依赖全部 `ok` 返回 HTTP 200，否则返回 HTTP 503。

### 11.2 MQTT 派单下发报文（`robot/{robot_id}/task`）

`MqttClient.publish_task` 下发到 `robot/{robot_id}/task`（QoS1），报文为
冻结命令信封兼容格式（冻结契约真源：`docs/device-interface.md` §3）：

```json
{
  "command_id": "cmd_tsk_20260919_ab12cd",
  "device_id": "RBT-001",
  "seq": 1,
  "action": "dispatch",
  "issued_at": 1758230400.0,
  "issued_at_iso": "2026-09-19T00:00:00+00:00",
  "expires_at": 1758230430.0,
  "expires_at_iso": "2026-09-19T00:00:30+00:00",
  "params": {
    "task_id": "tsk_20260919_ab12cd",
    "target": { "lng": 119.66, "lat": 26.39 },
    "priority": 1
  },
  "task_id": "tsk_20260919_ab12cd",
  "target": { "lng": 119.66, "lat": 26.39 },
  "priority": 1
}
```

- `command_id = cmd_{task_id}`：稳定幂等键；`seq` 同设备单调递增；
  `expires_at = issued_at + 30s`（TTL 默认 30 秒，可配置）。
- 顶层 `task_id / target / priority` 为向后兼容字段；`issued_at` 为信封
  字段（epoch 秒），需要 ISO 字符串时读取 `issued_at_iso`。

---

## 十二、AI 推理服务（独立端口 8081）

| 接口 | 说明 |
| --- | --- |
| `GET /infer/health` | 返回 `{ status, backend, model_version, classes }` |
| `POST /infer/detect` | 上传图片（multipart），返回 `detections` + `aggregate` |
| `POST /infer/reload` | 热加载模型，不重启服务 |

`POST /infer/detect` 响应：
```json
{
  "code": 0,
  "data": {
    "detections": [ { "class": "foam", "confidence": 0.91, "bbox": [412, 288, 468, 331] } ],
    "aggregate": { "main_class": "foam", "count": 1, "max_confidence": 0.91 },
    "model_version": "det_v0.1.0",
    "backend": "onnxruntime:CPUExecutionProvider",
    "elapsed_ms": 42.7
  }
}
```

返回的 `detections` 结构**与边缘端事件报文中的同名字段一致**，边缘盒可以直接组装上报，不需要转换。

---

## 十三、调试速查

```bash
# 健康检查
curl http://localhost:8000/health

# 事件列表
curl "http://localhost:8000/api/v1/events?hours=24&page_size=5"

# 热力图
curl "http://localhost:8000/api/v1/events/heatmap?hours=24&grid_size=500"

# 大屏指标卡
curl http://localhost:8000/api/v1/stats/dashboard

# 上报一个事件（触发自动派单）
curl -X POST http://localhost:8000/api/v1/events \
  -H "Content-Type: application/json" \
  -d '{
    "event_id": "evt_test_001",
    "device_id": "CAM-MABI-01",
    "device_type": "shore_camera",
    "timestamp": "2026-09-18T00:10:00+08:00",
    "location": {"lng": 119.6531, "lat": 26.3867},
    "detections": [{"class": "foam", "confidence": 0.91, "bbox": [412,288,468,331]}],
    "aggregate": {"main_class": "foam", "count": 3, "max_confidence": 0.91},
    "seq": 999001
  }'

# 手动推进任务状态
curl -X PATCH http://localhost:8000/api/v1/tasks/tsk_xxx \
  -H "Content-Type: application/json" \
  -d '{"status": "collecting"}'

# 端到端冒烟测试（一条命令跑完整个闭环）
python scripts/smoke_test.py
```

---

相关文档：`architecture.md`（架构与数据流）· `mqtt-topics.md`（MQTT 主题树）· `deployment.md`（部署）· `development.md`（协作）
