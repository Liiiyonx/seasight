# 开发规范与协作流程

> 三个人三周做出可演示系统，靠的不是写得多，而是**接口先冻结、互不阻塞**。

---

## 一、分组与职责边界

| 组 | 人 | 主战场 | 交付物 | 硬接口 |
| --- | --- | --- | --- | --- |
| **平台组** | 1~2 人 | `backend/` + `frontend/` | 可运行平台、大屏、工单流转 | 提供 HTTP 接口 + WebSocket |
| **算法组** | 1~2 人 | `ml/` + `edge/` | 模型权重、边缘推理程序 | **遵守 `mqtt-topics.md` 报文契约** |
| **硬件组** | 1 人 | 结构 + 机器人本体 | 概念样机、演示视频 | 遵守派单/回传消息格式 |

### 边界的铁律

> **接口先冻结，实现后填。**

三组在 W1 结束前必须就三件事达成一致并写进文档：

1. **MQTT 主题与报文结构**（已冻结 → `mqtt-topics.md`）
2. **事件 JSON 的字段名与类型**（已冻结 → `api.md` 第三节）
3. **任务状态机**（已冻结 → `api.md` 第四节）

冻结之后，算法组可以对着 `edge/simulator/` 写真实边缘程序，硬件组可以对着 `robot/{id}/task` 报文写解析代码，平台组可以对着同一个 JSON 写入库逻辑——**三方都不需要等对方写完**。

任何一方想改字段，走第二节的流程，不允许"我先改了我这边，你们跟上"。

---

## 二、接口变更流程

```
① 提出方在 Issue 里写清楚：改哪个字段、为什么、影响谁
        ↓
② 三方确认（口头/群里都行，但要有结论）
        ↓
③ 改文档（先改 docs/ 下对应的 .md）
        ↓
④ 三方各自改代码
        ↓
⑤ 跑 `python scripts/smoke_test.py` 验证端到端没破
```

**为什么必须先改文档**：文档是唯一的共识载体。如果只改代码，另外两人会继续用旧格式发/收，故障会以"某条消息莫名其妙丢了"的形式出现，排查成本极高。

### 兼容性规则

| 改动 | 是否需要三方同步 | 说明 |
| --- | --- | --- |
| 新增可选字段 | ❌ 不需要 | 老代码忽略未知字段即可。但要确认接收方**不会因未知字段报错** |
| 新增必填字段 | ✅ 需要 | 会导致老发送方报文校验失败 |
| 改字段名 | ✅ 需要 | 破坏性 |
| 改字段类型 | ✅ 需要 | 破坏性 |
| 删除字段 | ✅ 需要 | 破坏性 |
| 改枚举值 | ✅ 需要 | 特别注意：SQL 里的 enum 类型改起来要迁移 |

**建议**：宁可多两个可选字段，也不要为了"干净"删字段。备赛期没有那么多时间做迁移。

---

## 三、分支与提交

### 3.1 分支模型

```
main                    ← 只由负责人合并，保持随时可演示
 ├── feat/platform-*    ← 平台组
 ├── feat/edge-*        ← 算法组（边缘）
 ├── feat/ml-*          ← 算法组（训练）
 └── fix/*              ← 修 bug
```

**规则**：

- 每人独立分支，**不直接推 main**
- 合并前必须先本地跑通 `make smoke`
- 合并到 main 的功能必须是**能演示的**，不接受"半成品先合进去"

### 3.2 提交信息

```
<type>: <简短描述>

[可选正文：为什么这么改]
```

| type | 用途 |
| --- | --- |
| `feat` | 新功能 |
| `fix` | 修 bug |
| `docs` | 只改文档 |
| `refactor` | 重构（行为不变） |
| `chore` | 构建、依赖、配置 |
| `test` | 测试 |

示例：

```
feat: 派单引擎增加同区域防抖合并

不加防抖时同一片水域 3 分钟内会派 10 趟，机器人空驶严重。
按 200m + 10 分钟窗口合并到已有活跃任务。
```

**提交信息写"为什么"比写"做了什么"重要**。diff 已经说明了做了什么。

---

## 四、代码规范

### 4.1 Python（后端 / 算法）

| 项 | 规范 |
| --- | --- |
| 版本 | 3.11+（`from __future__ import annotations` 兼容写法） |
| 格式化 | 行宽 100，4 空格缩进 |
| 类型标注 | **函数签名必须标注**（含返回类型） |
| 命名 | 模块/函数 `snake_case`，类 `PascalCase`，常量 `UPPER_CASE` |
| 异步 | I/O 一律 async；**禁止在 async 函数里用阻塞调用** |

**分层硬规则**（违反即评审不通过）：

| 层 | 允许 | 禁止 |
| --- | --- | --- |
| `api/v1/` | 参数校验、编排、schema 转换 | 写 SQL、写业务分支 |
| `services/` | 业务逻辑、状态机、调 repository | 直接写 SQL |
| `repositories/` | 查询构造、空间查询 | 业务判断 |
| `models/` | 表结构、常量类 | 任何逻辑 |
| `mqtt/` | 报文解析、转发到 service | 持有请求级 session |

**为什么这么严**：派单逻辑有三个入口（MQTT、HTTP 备用、定时补派）。一旦逻辑写进接口层，必然出现三份有细微差异的实现——这是这类项目最典型、也最难查的技术债。

### 4.2 异步陷阱清单

```python
# ❌ 错：阻塞事件循环
async def handler():
    time.sleep(1)                    # 整个服务卡 1 秒
    data = requests.get(url)         # 同上

# ✅ 对
async def handler():
    await asyncio.sleep(1)
    async with httpx.AsyncClient() as client:
        data = await client.get(url)
```

```python
# ❌ 错：并发下应用层预查会漏
if not await repo.exists(device_id, seq):
    await repo.create(...)           # 两个协程同时通过检查 → 都插入

# ✅ 对：数据库唯一约束兜底 + 捕获冲突
try:
    await repo.create(...)
except IntegrityError:
    return  # 正常路径（QoS1 重传）
```

> **QoS1 重复投递是正常行为，不是异常**。判重逻辑必须能优雅处理"并发插入冲突"，而不是让它冒泡成 500。

### 4.3 JavaScript / Vue（前端）

| 项 | 规范 |
| --- | --- |
| 框架 | Vue 3 **组合式 API** + `<script setup>` |
| 缩进 | 2 空格 |
| 组件命名 | 文件 `PascalCase.vue`（`VideoPlayer.vue`） |
| 工具函数 | `camelCase.js` |
| 状态 | Pinia；**跨页共享**才放 store，页面内状态留组件内 |
| 请求 | 统一走 `src/api/index.js`，不在组件里写 axios |

**Composition API 的两个必守点**：

```js
// ❌ 错：ECharts / mpegts 实例泄漏
onMounted(() => {
  const chart = echarts.init(el.value)
  chart.setOption(option)
  // 组件卸载了 chart 还在，切换路由几次内存就炸了
})

// ✅ 对
let chart = null
onMounted(() => {
  chart = echarts.init(el.value)
  chart.setOption(option, true)
})
onUnmounted(() => {
  chart?.dispose()
  chart = null
})
```

```js
// ❌ 错：WebSocket 在组件卸载后继续跑，还握着旧组件的回调
onMounted(() => {
  const ws = new WebSocket(url)
  ws.onmessage = e => events.value.push(JSON.parse(e.data))
})

// ✅ 对：走 utils/realtime.js，它内部处理了 onUnmounted 自动关闭
const { connected } = useRealtime({ onMessage: msg => { ... } })
```

**`chart.setOption(option, true)`** 的第二个参数 `true` 表示**不合并**。不加的话，切换筛选条件时旧数据会残留（比如饼图从 5 个扇区变 3 个，会显示成 3 个新 + 2 个旧的）。

### 4.4 SQL

```sql
-- ✅ 空间查询：粗筛走索引 + KNN 排序
SELECT r.device_id
FROM t_device r
WHERE r.device_type = 'robot'
  AND r.status = 'online'
  AND ST_DWithin(r.location::geography, e.location::geography, 3000)  -- 米制粗筛，走 GiST
ORDER BY r.location <-> e.location                                     -- KNN 算子，走 GiST
LIMIT 1;

-- ❌ 反例：对每行算距离再排序，无法走索引，全表扫描
SELECT r.device_id
FROM t_device r
ORDER BY ST_Distance(r.location::geography, e.location::geography)
LIMIT 1;
```

**聚合必须投影**：

```sql
-- ❌ 错：ST_SnapToGrid 作用在 4326 上，网格单位是「度」
--        在 26°N 附近 1 度经度 ≈ 100km，网格毫无意义
SELECT ST_SnapToGrid(location, 500) FROM t_event;

-- ✅ 对：投影到 3857（米制）再聚合
SELECT ST_Transform(ST_SnapToGrid(ST_Transform(location, 3857), 500), 4326)
FROM t_event;
```

**状态变更必须走服务层**：

```python
# ❌ 错：绕过状态机校验
task.status = "done"

# ✅ 对：唯一合法入口
await DispatchEngine(session).transition(task, TaskStatus.DONE)
```

---

## 五、本地开发环境

### 5.1 首次准备

```bash
git clone <仓库地址> seasight
cd seasight
cp .env.example .env        # 填密码
docker compose up -d        # 起依赖
make db-init                # 建表 + 种子数据
```

### 5.2 日常开发

```bash
make up                 # 起依赖服务
make dev-backend        # 后端热重载（终端 1）
make dev-frontend       # 前端热重载（终端 2）
make simulate           # 模拟边缘盒（终端 3）
```

改后端或前端代码会自动重载，改 `.env` 需要重启进程。

### 5.3 常用命令

```bash
make help               # 列出全部命令
make ps                 # 服务状态
make logs               # 跟随日志
make smoke              # 端到端冒烟测试
make test               # 仓库全量测试（backend + edge + ml）
make clean              # 清 Python 缓存
make db-reset           # ⚠️ 清空数据库重建（会丢数据）
```

### 5.4 不要提交的东西

`.gitignore` 已排除，但交付前请确认：

| 项 | 原因 |
| --- | --- |
| `.env` | **含密钥** |
| `node_modules/` | 体积 + 平台差异 |
| `ml/datasets/**` | 数据集体积大，另存网盘 |
| `*.pt` / `*.onnx` / `*.engine` | 模型权重体积大 |
| `runs/` | 训练产物 |
| `__pycache__/` | 编译缓存 |
| `edge/simulator/.simulator_state.json` | 运行时状态 |
| `logs/` | 日志 |

**提交前跑一次**：

```bash
git status --short
git diff --cached --stat
# 尤其检查：有没有 .env、有没有带密码的配置、有没有大文件
```

---

## 六、测试策略

### 6.1 三层测试

| 层 | 位置 | 跑法 | 覆盖 |
| --- | --- | --- | --- |
| 单元测试 | `edge/simulator/test_temporal.py`、`backend/tests/` | `pytest` | 纯逻辑：时序校验、状态机、NMS |
| 冒烟测试 | `scripts/smoke_test.py` | `make smoke` | 端到端：事件→派单→状态流转 |
| 人工验证 | 浏览器 | — | 大屏、地图、视频、看板 |

### 6.2 冒烟测试做什么

`scripts/smoke_test.py` 按顺序验证七件事：

1. 后端健康检查可达
2. 设备列表非空（种子数据已导入）
3. 上报一条高优先级事件 → 返回 `code=0`
4. **重复上报同一条 → 返回 `duplicate=true`**（幂等生效）
5. 事件列表里能查到刚上报的事件
6. 派单后能查到对应任务
7. 任务状态机推进：`assigned → navigating → collecting → done`

任何一步失败，给出明确的中文原因与排查建议。

### 6.3 时序校验的回归测试

`edge/simulator/test_temporal.py` 是本项目**最有价值的一组测试**，因为它守着一个容易被悄悄改坏的核心逻辑：

| 测试 | 守住什么 |
| --- | --- |
| `test_continuous_target_confirmed` | 连续命中能升级为确认事件 |
| `test_transient_noise_suppressed` | 随机噪声不会误确认 |
| `test_class_mismatch_not_matched` | 不同类别不会互相"续命" |
| `test_iou_threshold_rejects_far_box` | 距离近但无重叠的框不会被错误关联 |
| `test_miss_decay_removes_track` | 目标消失后会被淘汰 |
| `test_noise_filtered_by_confirmation_rate` | 混合场景只确认持续目标 |
| `test_decimation_suppresses_noise` | 抽帧推理下噪声被压制、目标仍被确认 |
| `test_suppression_ratio_in_range` | **抑制率恒在 0~100%，且 fed = absorbed + suppressed** |
| `test_seq_monotonic` | seq 单调递增 |

最有力的一项实测结果：**300 条原始检测 → 仅 1 个确认事件**。

跑法：

```bash
cd edge/simulator
python test_temporal.py
```

> **改时序校验参数后必须跑这组测试**。`match_distance` 与 `min_iou` 调松一点，噪声就会"碰巧"关联上；调紧一点，真实目标又会被拆成多个。这组测试是唯一能快速发现踩线的工具。

### 6.4 模拟器提供的测试能力

| 场景 | 命令 | 验证什么 |
| --- | --- | --- |
| 正常演示 | `--scenario demo --event-interval 3 --loop` | 全链路（3 秒冷却，演示节奏） |
| 压测派单 | `--scenario stress` | 派单引擎并发 |
| 可复现回归 | `--scenario regression --seed 42` | 固定种子，结果可比对 |
| 幂等 | `--dup-rate 0.3` | 判重是否真生效 |
| 断网补传 | `--net-drop-every 8` | 本地队列与补传顺序 |
| 看报文 | `--dry-run` | 不进 broker，只打印 |

---

## 七、联调节奏

### 推进顺序（里程碑）

1. **接口冻结**（本文档三节）—— 前后端/边缘三方按同一份契约并行开发
2. 模拟器上报 → `t_event` 入库成功
3. 事件 → 自动生成任务 → MQTT 下发到机器人
4. 机器人回传 → 任务置 `done` → 大屏更新

### 联调的三道关

| 关卡 | 判据 | 谁阻塞谁 |
| --- | --- | --- |
| 关 1：报文通 | 模拟器上报 → `t_event` 有记录 | 算法组等平台组建完表 |
| 关 2：派单通 | 事件 → 自动生成任务 → MQTT 下发 | 平台组等算法组确认 ACK 格式 |
| 关 3：闭环通 | 机器人回传 → 任务 `done` → 大屏更新 | 三方都要参与 |

**每过一关就录一次屏**。备赛最后阶段最怕"好像能跑但没证据"，录屏是最省的保险。

---

## 八、文档维护

| 文档 | 更新时机 | 负责人 |
| --- | --- | --- |
| `README.md` | 架构/技术栈/命令变化 | 负责人 |
| `docs/architecture.md` | 分层、数据流、关键决策变化 | 平台组 |
| `docs/api.md` | **任何 HTTP 接口变化** | 平台组 |
| `docs/mqtt-topics.md` | **任何报文结构变化** | 提出变更的一方 |
| `docs/deployment.md` | 部署方式、依赖版本变化 | 平台组 |
| `docs/development.md` | 规范与流程变化 | 负责人 |
| `docs/decisions.md` | 每个重要技术决策（新增时追加一条 ADR） | 做决策的人 |

**文档缺失是备赛项目最常见的失分点**。答辩时被问"你们怎么保证误报率"，如果只能说"我们试了一下还行"，和能掏出实测抑制率数据，是两个档次。

---

## 九、演示（Demo）准备

### 9.1 演示前一天的检查

- [ ] `docker compose up -d` 全部 healthy
- [ ] `make db-reset && make db-init` 重置到干净种子数据（保证每次演示起点一致）
- [ ] 三台设备电量/仓容是合理值（不要让机器人电量显示 3%）
- [ ] 大屏在投影分辨率下布局正常（**1920×1080 一定要测**）
- [ ] 浏览器缓存已清（避免看到旧版前端）
- [ ] 视频流能播（若现场无真实摄像头，准备好演示视频文件）
- [ ] 断网预案：手机热点 + 提前下载好的录屏

### 9.2 演示脚本（3 分钟版）

| 时间 | 动作 | 说什么 |
| --- | --- | --- |
| 0:00–0:20 | 打开大屏 | 这是连江沿海 6 个点位的实时监测 |
| 0:20–0:50 | 启动模拟器，告警流出现新事件 | 边缘盒识别到泡沫浮球碎片，注意它不是逐帧报警——时序校验要求连续命中才升级 |
| 0:50–1:20 | 切到工单看板，新卡片出现 | 平台自动派单，选了最近的 RBT-001。这里做了防抖，同一片水域 10 分钟内只派一趟 |
| 1:20–1:50 | 点开工单详情，展示时间戳链 | 从发现到派单 2 秒，从派单到机器人确认 3 秒，全链路可追溯 |
| 1:50–2:20 | 切到热力图 | 颜色深的是高发区。注意这是密度不是总数，否则大网格会天然更热 |
| 2:20–2:40 | 切到报表页 | 治理量化：本周清理 62kg，马鼻镇占 45% |
| 2:40–3:00 | 回大屏 | 这一切的前提是误报率压得住——合成链路时序抑制率 76%（非识别精度） |

### 9.3 三个高频提问的准备

| 提问 | 回答要点 |
| --- | --- |
| **误报率多少** | 报时序链路抑制率 76%（被过滤检测数 / 输入检测数），说明机制与分母；声明它不是识别精度，真实海域精度待独立测试 |
| **和 WasteShark 区别** | 他们是单机产品，我们是区域级三级系统；识别对象是连江特色垃圾（EPS 子安碎片、废旧渔具），不是通用垃圾 |
| **机器人怎么分拣** | 打捞即三仓粗分（泡沫/塑胶/混合），岸基精分。**仓容是遥测回传的真实数据**，不是摆设 |
| **成本** | 国产化路线；WasteShark 报价按 F-05 引用，约 2.36 万美元起（E1），1:7.2 折算约 17 万元为 E0 假设，待询价验证 |

---

## WP-17 集成验收（浏览器 E2E + 受控故障演练）

> 证据等级上限：**E1**。浏览器 E2E 是本机代码 + 真实后端服务的集成验收，
> 最多是 E1，**不是真实用户试点**；故障包是受控注入测试，**不是真实
> broker / 硬件 / 公网故障**。不得据此宣称 E3/E4。

### 前置服务（必须在线；本包不自行启动/停止已有服务）

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| 前端 dev server | `http://127.0.0.1:5174` | `/api` 代理到后端 |
| 后端 | `http://127.0.0.1:8001` | 数据库/Redis 在线即可；MQTT 断开正是降级场景要验证的状态 |
| 系统 Chrome | `C:\Program Files\Google\Chrome\Application\chrome.exe` | 不下载 Playwright 自带浏览器 |
| playwright-core | 全局安装，经 `PLAYWRIGHT_CORE_PATH` 指向入口 | 缺失时脚本打印修复命令并以退出码 1 中止 |

### 命令

```powershell
# 1) 前端生产构建（验证产物可构建）
cd frontend; npm run build

# 2) 浏览器集成验收（E1）
$env:PLAYWRIGHT_CORE_PATH='<全局 playwright-core 安装路径>'
node scripts/browser_acceptance.mjs

# 3) 受控故障演练（E1；可选 live probe：设置 SEASIGHT_BACKEND_URL 后只读观测 /health）
$env:SEASIGHT_BACKEND_URL='http://127.0.0.1:8001'
.\.venv-analysis\Scripts\python.exe scripts\fault_acceptance.py

# 4) Makefile 封装（保持既有目标不变）
make test-browser
make fault-acceptance
```

### 产物

| 产物 | 说明 |
| --- | --- |
| `artifacts/browser-acceptance/latest.json` | 全量结果：generated_at / frontend_url / backend_url / evidence_level=E1 / scope_note / results / summary / observation.api_paths |
| `artifacts/browser-acceptance/*.png` | 各场景全页截图（稳定文件名，可找） |
| `artifacts/browser-acceptance/tasks-export.csv`、`reports-export.csv` | 真实导出的工单/日报 CSV（BOM、字节数、SHA256 记录在 JSON） |
| `artifacts/browser-acceptance/admin-storage.json`、`viewer-storage.json` | 真实登录后的 storageState，供同角色上下文复用 |
| `artifacts/fault-acceptance/latest.json` | 故障包逐组结果（command / returncode / parsed_summary / passed）+ live_probe（未配置时为 null） |

### 证据上限（如实声明）

- 浏览器 E2E 走**真实登录**（登录页表单 + 真实 `POST /api/v1/auth/login`）、
  **真实业务读取链路**（dashboard / events / tasks / devices / reports / agents 六页）、
  **真实 CSV 导出**、**真实 `/health` 降级探针**；仅 Agent 页交互场景允许
  `page.route` 拦截以稳定构造「活跃运行 / waiting_approval」状态，结果中逐项
  标注 `mocked: true` 与原因。
- 以上均为 **E1**（有代码、自动化验收、确定性用例），不是真实用户试点、
  真实设备接入、真实海域或真实网络压测。
- 故障包是受控测试（进程内注入 / 内存 transport / scratch PostgreSQL），
  不代表真实 broker、公网、硬件或现场故障。

### 最近一次本机实测（2026-09-22）

| 检查 | 结果 |
| --- | --- |
| 前端生产构建 | `npm run build` 通过，681 个模块完成转换 |
| 浏览器集成验收 | 15/15 通过；12 个无拦截场景，3 个 Agent 场景 `mocked: true` |
| 受控故障演练 | 4/4 组、127/127 用例通过，0 skipped |
| 契约漂移 | 通过 26、警告 0、失败 0 |
| 宣称门禁 | `CLEAN`，10 条红线命中 0 |
| 全量 pytest | 1061 passed / 0 skipped / 0 failed / 1 warning / 91.63s |

---

## WP-18 Agent 真实状态端到端验收（E2 软件集成）

> 证据等级：**E2**。使用合成塑料垃圾事件，但走真实 HTTP、登录与角色权限、
> PostgreSQL Agent 仓储、工单/事件写入、WRITE 审批、幂等重放和进程重启后
> 只读回放。AgentRuntime 仍在进程内；`t_agent_run_state` 是审计与重启只读
> 回放镜像，不声称未完成 run 可续跑，也不构成生产级、真实设备或真实海域证据。

### 命令

```powershell
.\.venv-analysis\Scripts\python.exe scripts\agent_real_state_acceptance.py
```

脚本自行启动和停止验收后端，只允许占用 `127.0.0.1:8011`；验收进程设置
`BACKGROUND_WORKERS_ENABLED=false`，避免自动派发 worker 与显式 Agent 调用竞争。

### 覆盖范围

| 阶段 | 关键验收 |
| --- | --- |
| phase1-baseline | 真实 HTTP 事件接入、operator/admin/viewer 权限、成功轨迹、真实工单创建、事件 `dispatched`、同 idempotency key 重放 |
| phase2-approval | WRITE 动作进入 `waiting_approval`，admin 审批后 run `succeeded`，工单和事件落库 |
| phase3-restart-readback | 重启进程后读取已完成 run/steps/task_result，并从数据库重放相同幂等键 |

直接数据库证据：2 个 succeeded run、25 个 steps、2 个 run state、2 个 task、
2 个 dispatched event、1 个 approved 审批；验收总计 129 项检查全部通过。

### 最近一次门禁复核（2026-09-22）

```text
[WP-18] PASS checks=129 run1=run_5c706c14dcc1 run2=run_d16e5ec8fe12
[WP-18] evidence=artifacts/agent-real-state-acceptance/latest.json
```

同时复跑的门禁结果：全量 pytest **1061 passed / 0 skipped / 0 failed /
1 warning / 91.63s**；浏览器集成 **15/15**；受控故障演练 **4/4 组、
127/127**；契约漂移 **26 passed / 0 failed**；宣称扫描 **CLEAN**。

### 证据上限（如实声明）

- 事件和模型输入是合成的，AgentRuntime 在当前进程中执行，不是分布式 Worker。
- 重启验收只覆盖已完成 run 的读取、轨迹回放和幂等重放，不覆盖未完成 run 续跑。
- 不包含真实摄像头、真实海域、真实用户、真实机器人、真实订单或生产级 SLA。

---

相关文档：`architecture.md`（架构）· `api.md`（接口契约）· `mqtt-topics.md`（MQTT 主题）· `deployment.md`（部署）
