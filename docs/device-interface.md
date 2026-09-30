# 设备孪生接口规范（WP-14 作业设备数字孪生与故障注入）

> 版本：1.1（2026-09-19，WP-14E：设备序号持久化 + 启动恢复接线）
> 工作包：WP-14 ｜ 证据等级：**E1/E2**
> 代码位置：`edge/device_sim/`
> 上位规格：`docs/agent-program-wave3.md` §3.5 / §8

本包建立**独立于感知事件模拟器**（`edge/simulator/`）的机器人作业单元
数字孪生，用于验证「命令 → ACK → 遥测 → 故障 → 恢复」闭环。

**边界声明（重要）**：本模块是确定性孪生与故障演练设施。它不代表真实
船舶、真实边缘盒、真实 MQTT 设备接入，也未做过现场验证。任何把孪生
行为写成「已接真船 / 真边缘盒 / 现场联调通过」的表述均属越级宣称。

---

## 1. 模块结构

| 文件 | 职责 |
| --- | --- |
| `protocol.py` | 冻结协议：命令 / ACK / 遥测 / 故障码常量、领域对象、平台侧 ACK 幂等（`AckTracker`）、假时钟与确定性 ID 工厂 |
| `transport.py` | 可注入传输抽象（`DeviceTransport`）+ 内存 transport（`MemoryTransport`） |
| `device.py` | `DeviceTwin` 状态机：命令处理、断网排队、重连补传、急停优先、故障注入/恢复、遥测序号跨进程持久化（`load_seq_from_file` / `save_seq_atomic`） |
| `faults.py` | 脚本化场景（`DeviceScenario`）、确定性执行器（`run_device_scenario`）、报告（`FaultReport`） |
| `test_device.py` / `test_faults.py` | 单元与场景测试（内存 transport，无网络/无真实设备） |
| `test_seq_persistence.py` | 遥测序号跨进程持久化测试（写盘 → 新进程重载 → 严格单调；默认不写盘） |

---

## 2. 主题树（对齐 `docs/mqtt-topics.md` 的 robot 通道）

```
robot/{device_id}/cmd          平台 → 设备  通用命令（QoS1）
robot/{device_id}/task         平台 → 设备  派单 dispatch（QoS1，对齐后端 publish_task）
robot/{device_id}/cmd/ack      设备 → 平台  命令回执（QoS1，后端订阅 robot/+/cmd/ack）
robot/{device_id}/telemetry    设备 → 平台  遥测（QoS0）
```

后端订阅通配符已含 `robot/+/cmd/ack`（见 `backend/app/mqtt/topics.py`）。

---

## 3. 命令信封（冻结五字段）

所有命令（六个动作）共用同一信封，缺一不可：

```json
{
  "command_id": "cmd_0001",
  "device_id": "RBT-001",
  "seq": 1,
  "issued_at": 1758230400.0,
  "expires_at": 1758230430.0,
  "action": "dispatch",
  "params": {
    "task_id": "tsk_20260919_ab12cd",
    "target": { "lng": 119.66, "lat": 26.39 }
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `command_id` | string | 命令幂等键；同 ID 重复到达只执行一次 |
| `device_id` | string | 目标设备 |
| `seq` | int | 该设备的命令序号（平台侧单调递增） |
| `issued_at` | float | 下发时间（epoch 秒；报文另有 `*_iso` 字段） |
| `expires_at` | float | 过期时间；处理时已过期则拒绝并回 rejected ACK |
| `action` | enum | 见下表 |
| `params` | object | 动作参数 |

冻结动作：

| 动作 | 行为 | 参数 |
| --- | --- | --- |
| `dispatch` | 派单：设置任务号与目标点，进入 `navigating` | `task_id`、`target:{lng,lat}`（缺目标则拒绝） |
| `pause` | 暂停：`navigating/collecting/returning` → `paused` | — |
| `resume` | 恢复：`paused` → 暂停前模式；`emergency_stop` → 解除急停并恢复 | — |
| `return_home` | 返航：→ `returning`；回到基地卸仓、清任务号、回 `idle` | — |
| `emergency_stop` | 急停：无条件最高优先级，进入 `emergency_stop` 模式 | — |
| `ack` | 保留动作：回执确认（`ack_echo`），不触发模式迁移 | — |

---

## 4. 遥测（冻结最小字段）

```json
{
  "device_id": "RBT-001",
  "seq": 12,
  "timestamp": 1758230412.0,
  "mode": "collecting",
  "battery": 87,
  "position": { "lng": 119.6612, "lat": 26.3903 },
  "velocity": 1.5,
  "bin_usage": { "foam": 0.42, "plastic": 0.18, "mixed": 0.09 },
  "mission_id": "tsk_20260919_ab12cd",
  "fault_code": "none"
}
```

固定最小字段：`device_id, seq, timestamp, mode, battery, position,
velocity, bin_usage, mission_id, fault_code`（`protocol.TELEMETRY_FIELDS`）。

- `battery`：0–100 整数。作业中（navigating/collecting/returning）每个
  tick 按 `battery_drain_per_tick` 下降，idle/paused/e_stop 缓慢回升。
- `bin_usage`：三仓占用率 dict（0–1）。`collecting` 时上升，返航回基地
  卸仓归零。
- `position`：WGS84 `{lng, lat}`；`gps_lost` 故障时置 `None`。
- `timestamp`：epoch 秒，随孪生时钟单调推进（确定性测试用假时钟）。
- `seq`：遥测序号，同设备单调递增；断网补传仍按原 seq 顺序到达。
- `fault_code`：主故障码，见第 6 节；无故障为 `"none"`。

### 4.1 遥测序号跨进程持久化（WP-14E）

遥测 `seq` 默认只存在于进程内存 —— 进程重启后若从 0 重新计数，平台侧
`(device_id, seq)` 判重会把重启后的首条遥测误判为旧数据。WP-14E 提供
可选的跨进程持久化：

- **路径配置**：构造参数 `DeviceTwin(..., seq_store_path=...)`，或环境变量
  `DEVICE_SIM_SEQ_FILE`（构造参数优先）。**默认不写盘**（未配置时行为与
  旧版完全一致，既有测试与调用不受影响）。
- **写盘时机与原子性**：每个遥测 tick 自增后同步落盘；落盘采用**原子写**
  （同目录临时文件 + `os.replace` 替换），不留半截文件、不留 `.tmp` 残留；
  文件内容为 JSON `{"seq": N}`。
- **重启语义**：新进程构造时读取上次持久化的序号并从**该值继续递增**，
  下一 tick 严格大于已用序号 —— 不回退到 0、不重复已用序号。
- **降级不崩溃**：文件缺失 = 首次运行（正常从 0 开始）；文件损坏以
  warning 降级，从**最后一个可解析值**（取不到则 0）继续；写盘失败仅
  内存推进（下个 tick 重试）。任何情况都不抛异常。
- 底层辅助函数：`device_sim.load_seq_from_file(path) -> (seq, status)`（
  status ∈ `ok / missing / corrupt`）与 `device_sim.save_seq_atomic(path, seq)
  -> bool`。

> **边界**：持久化对象是孪生的本地文件，与平台侧 `t_task_ack` 账本无关；
> 它解决「设备侧序号重启回退」，平台侧判重水位恢复见
> `docs/mqtt-topics.md` §5.5 与 `docs/architecture.md` §七。

---

## 5. ACK 与幂等

回执报文：

```json
{
  "ack_id": "ack_0001",
  "command_id": "cmd_0001",
  "device_id": "RBT-001",
  "seq": 1,
  "received_at": 1758230401.0,
  "accepted": true,
  "reason": "dispatched",
  "mode": "navigating"
}
```

平台侧按 `command_id` 幂等去重（平台实现位于
`backend/app/mqtt/ack.py::AckTracker`，语义与本协议一致、不 import
edge/device_sim；接线见 §11），三类异常都有确定性判定（优先级
duplicate > late > out_of_order > new）：

- **重复 ACK**（`duplicate`）：同一 `command_id` 再次到达 → 返回首次
  存储的规范回执，不重复生效。来源包括：平台重复下发同一命令（设备
  幂等复回）、`duplicate_ack` 故障（设备重复投递同一回执）。
- **乱序 ACK**（`out_of_order`）：按**到达顺序**判定 —— 新回执的命令
  序号小于该设备已 ACK 的最大序号。判据是「已 ACK 水位」而非「已下发
  水位」，多命令在途时按序回执不会被误判。
- **超时 ACK**（`late`）：`received_at` 晚于命令 `expires_at`。回执仍
  被记录，平台据此判断「命令已过期但设备确实处理过」（配合
  `ack_timeout` 故障与 `command_processing_delay` 可复现）。

设备侧：同一 `command_id` 的重复命令不会重复执行，直接复回上次回执；
`ack_timeout` 故障期间完全抑制回执。

---

## 6. 故障码（冻结集合）

`ack_timeout, duplicate_ack, out_of_order_ack, battery_critical,
bin_full, gps_lost, communication_lost, emergency_stop`（外加 `none`）。

多故障并存时遥测 `fault_code` 只上报一个主故障，优先级从高到低：

```
emergency_stop > communication_lost > battery_critical > bin_full
> gps_lost > ack_timeout > duplicate_ack > out_of_order_ack
```

行为副作用：

| 故障码 | 注入副作用 | 恢复 |
| --- | --- | --- |
| `emergency_stop` | 强制进入急停模式（保存急停前模式） | `resume` 命令或 `clear_fault` 恢复 |
| `communication_lost` | 强制传输离线 → 出站进入 Outbox 排队 | `clear_fault` 重连并补传 |
| `battery_critical` | 遥测上报该码；作业中触发自动返航 | 返航后充电回升自动清除；或 `clear_fault` |
| `bin_full` | 遥测上报该码；collecting 时触发自动返航 | 卸仓后自动清除；或 `clear_fault` |
| `gps_lost` | 遥测 `position=None`，停止导航 | `clear_fault` |
| `ack_timeout` | 抑制所有回执（平台侧表现为无 ACK） | `clear_fault` |
| `duplicate_ack` | 每条回执重复投递两次 | `clear_fault` |
| `out_of_order_ack` | 同一批回执按倒序发布 | `clear_fault` |

天然故障（低电量、仓满）由状态机自动产生并自动恢复：电量低于阈值 →
自动返航 → 充电回升；仓满 → 自动返航 → 卸仓归零。恢复闭环对每个注入
故障记录 `injected_at / cleared_at`，并汇总 `device_fault_recovery_rate`
（已恢复注入故障数 / 注入故障总数；无注入故障时为 `null`）。

---

## 7. 断网排队与重连补传

- 出站（遥测、ACK）在传输断开时进入本地 **Outbox**（有界队列，满则丢
  最旧），计数 `buffered`。
- 重连后下一个 tick 按**原顺序**补传（计数 `flushed`），遥测 `seq` 在
  补传后仍严格递增 —— 平台 `(device_id, seq)` 判重与时间轴不回退。
- 离线期间平台下发的命令被丢弃（真实弱网语义：设备收不到）。

---

## 8. 紧急停止优先级

`DeviceTwin.tick()` 每批命令先取所有 `emergency_stop` 无条件插队执行，
再按 `seq` 升序处理其余命令。急停执行后进入 `emergency_stop` 模式，
同批其余非 `resume` 命令被拒绝（回执 `accepted=false,
reason=device_in_emergency_stop`）。`resume` 是急停的唯一命令恢复路径
（恢复后回到急停前模式）。

---

## 9. 故障注入场景与确定性报告

```python
from device_sim import DeviceScenario, run_device_scenario, TwinConfig

scenario = (
    DeviceScenario("demo")
    .cmd(1.0, "dispatch", task_id="t1", target={"lng": 119.66, "lat": 26.39})
    .inject(2.0, "gps_lost")
    .recover(4.0, "gps_lost")
    .cmd(5.0, "return_home")
)
report = run_device_scenario("RBT-001", scenario, seed=42, end_at=6.0)
print(report.to_json())
```

场景事件类型：`command` / `fault` / `recover` / `disconnect` / `connect`。
命令信封由执行器确定性生成（`cmd_{n:04d}`、seq 递增、`issued_at`、
`expires_at=issued_at+ttl`，`params["ttl"]` 可覆盖默认 30s）。

报告字段（JSON 可序列化、可哈希比对）：

```
scenario_id, device_id, seed, started_at, finished_at, steps, noise,
commands_issued / commands_processed / commands_rejected / commands_invalid,
acks_published / acks_suppressed,
ack_outcomes {new, duplicate, out_of_order, late},
telemetry_count / telemetry_buffered / telemetry_flushed,
fault_detected_telemetry / fault_observations {code: {samples, first_seen, last_seen}},
faults_injected [{fault, injected_at, cleared_at, recovered}],
faults_recovered, device_fault_recovery_rate,
noise_hash, events
```

**确定性**：固定 `seed` + 相同场景 → 报告逐字节相同。`noise=True` 时用
`random.Random(seed)` 给遥测加抖动，报告附 `noise_hash`（不同种子不同、
同种子可复现），用于证明种子确实影响输出。

---

## 10. 测试

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv-analysis\Scripts\python.exe -m pytest edge\device_sim -q
```

覆盖：传输可注入（内存 transport，无 MQTT）、命令信封五字段、命令 →
模式迁移闭环、ACK 幂等（重复/乱序/超时）、遥测最小字段与电量/仓容/
位置时间戳、断网排队与重连补传、急停优先级、故障注入确定性报告、
`device_fault_recovery_rate` 语义。

---

## 11. 集成状态（WP-14B：派单报文信封接线；WP-14C：ACK 消费端接线；WP-14D：ACK 审计账本）

- `backend/app/mqtt/client.py`：`MqttClient.publish_task` 已按 §3 冻结信封
  扩展下发报文（`robot/{device_id}/task`）：
  - 新增信封字段 `command_id`（=`cmd_{task_id}`，稳定幂等，QoS1 重投/换车
    重派同一任务时设备侧可去重）、`device_id`、`seq`（同 `device_id`
    进程内单调递增，不同设备独立计数）、`expires_at`（默认
    `issued_at + 30s`，TTL 可经 `publish_task(ttl=...)` 覆盖，与孪生
    `DEFAULT_COMMAND_TTL` 对齐）、`action="dispatch"`、`params`
    （内含 `task_id / target / priority`）；
  - 顶层同时保留向后兼容字段 `task_id / target / priority` 与 `issued_at`
    （信封字段，值为 epoch 秒；配套 `issued_at_iso / expires_at_iso` 提供
    ISO-8601 副本）。
  - WP-14C：**仅在 MQTT 发布成功后**把 `command_id / device_id / seq /
    expires_at` 登记到平台侧 `AckTracker`（`backend/app/mqtt/ack.py`），
    供 ACK 的 `late`（超时）判定；登记表为进程内内存，发布失败 / 进程
    重启后未登记命令仍可正常接收，但不会伪判 `late`（回落策略见 §5）。
- `backend/app/mqtt/ack.py`：平台侧 ACK 幂等与解析（WP-14C 新增，纯逻辑
  **不 import edge/device_sim**）：冻结回执信封解析（优先
  `ack_id/command_id/device_id/seq/received_at/accepted/reason/mode`，
  兼容旧字段 `task_id/robot_id/accepted/ts`）、`task_id` 反推
  （`command_id=cmd_{task_id}`，两者不一致整条拒绝）、判定优先级
  `duplicate > late > out_of_order > new`。
- `backend/app/mqtt/handlers.py`：`handle_robot_ack` 已消费冻结回执信封
  （`robot/{id}/cmd/ack`，§5 冻结字段为主、旧字段兼容）：
  - 无顶层 `task_id` 的冻结 ACK 按 `command_id=cmd_{task_id}` 反推并推进
    任务（修复「冻结 ACK 被静默丢弃」断链）；
  - 身份三重校验：`payload.device_id`（旧 `robot_id`）、topic `robot_id`、
    `task.robot_id` 必须一致，否则整条拒绝、不得推进；
  - `accepted=true` 仅当任务仍为 `assigned` 时经
    `DispatchEngine.transition` 推进 `navigating`；其他状态不重复推进；
  - `accepted=false` 首次回执且任务仍 `assigned` 时记录拒绝原因、清空
    `robot_id` 并 `assigned -> pending`，交由现有补派轮处理（handler 内
    不递归重派）；重复拒绝不重复回退；
  - 任何解析/处理异常隔离为 warning，不打断 MQTT 主循环。
  - WP-14D：账本写入与状态推进**同一事务**（先校验与判定、再写 ACK 行、
    最后提交；写库异常整体回滚，不留下「已推进但无回执证据」的状态）。
- `backend/app/models/task_ack.py` + `backend/app/repositories/__init__.py`：
  ACK 审计账本 `t_task_ack`（WP-14D）—— 一 `command_id` 一条规范回执
  （`outcome` 取 WP-14C 判定结果），重复到达只累计 `duplicate_count` 并
  更新 `last_duplicate_at / last_payload`，绝不覆盖首次规范回执
  （`raw_payload` 只写一次）；并发唯一冲突自动转 duplicate，不产生第二条
  规范行。`TaskAckRepository.rebuild_ack_tracker` 可从账本重建
  `AckTracker` 的规范回执与设备 ACK 水位（重启后 `duplicate` /
  `out_of_order` 判定可恢复；`late` 判定对「发布后未 ACK」的命令仍不可
  恢复 —— 内存登记表固有边界，不伪装持久化）。**WP-14E 已把该能力接入
  应用启动流程**（见下条 `backend/app/main.py`），重启后判重水位自动恢复。
- `backend/app/api/v1/tasks.py`：`GET /api/v1/tasks/{task_id}/acks` 审计
  查询（WP-14D）—— 仅登录用户可读；operator 只能查询本辖区任务；分页
  默认按 `received_wall_at DESC`；响应不回传 `raw_payload / last_payload`
  原始回执原文。
- `backend/app/main.py`：**启动恢复接线（WP-14E）** —— lifespan 启动阶段
  在 MQTT 客户端可用后调用 `recover_ack_tracker(mqtt_client.ack_tracker)`
  （内部即 `TaskAckRepository.rebuild_ack_tracker`）从 `t_task_ack` 重建
  判重水位：
  - 数据库不可用 / 表不存在 / 查询异常 → 降级 warning 并继续启动，绝不让
    启动失败（与 lifespan「每步独立 try」降级风格一致）；
  - 恢复幂等：重复启动不叠加、不报错（`rebuild_ack_tracker` 对已存在的
    规范回执只判重不覆盖，设备水位取 max）；
  - 耗时上界：整个重建包在超时（`ACK_TRACKER_REBUILD_TIMEOUT_SECONDS`，
    默认 5s）内，超时即放弃并降级，绝不让启动无限期挂起；
  - 可观测：结果（`status/rows/elapsed_ms/detail`）写入
    `app.state.ack_tracker_recovery`，`/health` 顶层暴露 `ack_recovery`
    字段，同时打日志，不静默。
  - **恢复边界（如实声明）**：`duplicate` / `out_of_order` 判定重启后可
    恢复；`late` 判定依赖发布时登记的 `expires_at`（t_task_ack 不存），
    「发布后未 ACK」的命令重启后**仍不可判 late** —— 属 WP-14C 内存登记
    表的固有边界，不宣称完整恢复；账本 + 任务状态守卫仍保证重复回执不
    重复推进。
- `backend/tests/test_ack_startup_recovery.py` / `backend/tests/test_ack_twin_loopback.py`：
  启动恢复（降级 / 幂等 / 超时 / 可观测 / 真实 scratch 库重建）与孪生闭环
  端到端用例（孪生 ACK → `handle_robot_ack` → 推进 + 落账 → 重启重建 →
  重放不重复推进；身份不一致 / 未知任务 / 未登记 deadline 负例）。
- `edge/arm_bridge/`：机械臂桥接层（E1/E2）—— 用 `ArmDriver` 隔离不同机械臂
  SDK（HTTP / 串口 / CAN 都可实现），桥接层订阅 `robot/{device_id}/task` 与
  `robot/{device_id}/cmd`，按本规范回 ACK、上报 progress，并把遥测发到
  `marine/{site_id}/{device_id}/telemetry`。`--dry-run` 在无硬件下跑通
  「派单 → ACK → collecting → done → 遥测」；自测只证明协议闭环，不构成
  物理拾取或现场验收。
- 以上为**后端侧消费接线 + 孪生本地闭环（E1/E2 证据等级）**，不代表真实
  设备接入完成；未做真实 MQTT / 网络 / 设备验证（验收全部为内存注入 +
  进程内调用，账本/迁移走真实 scratch PostgreSQL，见
  `backend/tests/test_mqtt_ack.py`、`backend/tests/test_task_ack_persistence.py`、
  `backend/tests/test_ack_twin_loopback.py`）。
- `backend/app/services/consumer.py`：消费端对 `robot/{id}/cmd/ack` 与
  `robot/{id}/telemetry` 的接入点（不在 WP-14B/14C 范围内改动）。
- `edge/simulator/**`：感知事件模拟器与本孪生保持独立；如演示需要把
  孪生命令/遥测并入同一 broker，由总控统一编排，**不得**在本包内
  耦合或改写模拟器。

### 11.1 平台工单执行仿真扩展（不是真实设备契约）

平台仿真页位于 `frontend/src/views/SimulationView.vue`，后端引擎位于
`backend/app/services/sim.py`，HTTP 控制面位于
`backend/app/api/v1/simulations.py`。仿真引擎只生成设备侧 ACK、遥测与
作业进度报文，并直接调用正式 `handle_robot_ack` / `handle_telemetry` /
`handle_robot_progress`，因此任务状态仍只由
`DispatchEngine.transition()` 推进，轨迹仍写现有 `t_track`，WebSocket
仍推送 `robot_status`。

为区分同任务的不同仿真会话，仿真 ACK 使用受控扩展：

```json
{
  "ack_id": "ack_sim_<run_id>",
  "task_id": "<task_id>",
  "command_id": "cmd_<task_id>:sim_<run_id>",
  "device_id": "<robot_id>",
  "seq": 0,
  "received_at": 1758230401.0,
  "accepted": true,
  "reason": "",
  "mode": "simulation"
}
```

平台仅在 `mode="simulation"`、顶层 `task_id` 存在且 `command_id` 精确匹配
`cmd_{task_id}:sim_{run_id}` 时接受；缺任一项整条拒绝。真实设备契约不变：
标准派单仍为 `command_id=cmd_{task_id}`，真机 ACK/命令/遥测字段无需携带
仿真标记，也不需要理解 `:sim_` 扩展。

仿真遥测沿用真实主题与最小字段，仅新增可选 `heading`（正北为 0°）供地图
显示朝向。结束作业时，`task/progress` 上报 `done`，末次遥测使用正式的
`status="idle"`；仿真不新增遥测状态枚举。

该能力是平台侧演练设施，不证明真实机器人、真实 MQTT Broker 或现场网络
已完成联调。删除仿真引擎并替换为真实设备报文源后，前端页面与 HTTP 契约
无需改变。

## 12. 已知边界

- 孪生为离散 tick 模型，物理量（电量、仓容、位移）按 tick 推进，不
  做连续时间积分。
- 内存 transport 不模拟 QoS 重传窗口与 broker 侧积压；重复投递由
  故障注入显式产生。
- 未接入真实设备协议栈（无串口/Modbus/CAN/真实 MQTT 客户端）；所有
  结论仅代表确定性孪生行为，证据等级 E1/E2。`edge/arm_bridge` 已提供
  驱动适配层与无硬件自测，真机械臂到货后的物理拾取结果仍只能标 E2
  （受控实验），不得写成现场验收。
