# 探海灵眸 SeaSight

> 海漂垃圾「感知—决策—执行」一体化智能治理系统

面向海洋养殖区海漂垃圾治理的三端协同智能系统：**岸基摄像头发现 → 平台自动派单 → 水面机器人清理 → 数据回传形成治理闭环**。

> 去掉机器人本体后，本项目仍可作为**“海漂垃圾治理决策与调度智能体软件”独立参加智能体开发比赛**。机器人只是可替换的 MQTT 作业端；软件侧已具备事件理解、规划、工具调用、策略守卫、审批、派单、MQTT/WebSocket 收尾、审计、回放和离线评测闭环。该口径不等于已交付完整机器人系统、真实海域验证或生产级运行。
>
> 同一代码基线还提供 `integrations/nexent/`：通过 stdio 或 Streamable HTTP MCP
> 把知识资产、动态本体、多跳检索、决策证据链、事件与工单能力暴露给 Nexent，
> 并附带 5 个可复用 Skills。出站认证支持专用账号登录与到期前自动刷新，
> 入站 HTTP 访问使用独立 Bearer Token。知识登记、本体审核、检索和决策追溯
> 均不依赖机器人。

本项目参加「数启海洋·智创未来」连江县首届创新创业大赛（赛道一：海洋科创，主攻方向③海上智能装备与无人系统）。

---

## 一、这是什么

一套**区域级治理系统**，不是单台清洁船。核心差异：

| 维度 | 市面竞品（WasteShark / Clearbot 等） | 探海灵眸 |
| --- | --- | --- |
| 形态 | 单机产品 | 感知—决策—执行三级系统 |
| 识别对象 | 通用漂浮垃圾 | 中国养殖区特色垃圾（EPS 泡沫浮球碎片、废旧渔具、饵料袋） |
| 分拣 | 无分类收集 | 打捞即三仓粗分 + 岸基精分 |
| 数据价值 | 清理量 | 热力图、派单调度、量化报表、溯源分析 |
| 成本 | WasteShark 起售价约 2.36 万美元（F-05，E1）；按 1 USD≈7.2 CNY 折算约 17 万元（折算为 E0 假设） | 国产化低成本路线 |

## 二、系统架构

```
┌──────────────────────────────────────────────────────────────┐
│  展示层  实时监测大屏 · 热力图 · 工单看板 · 报表中心 · 移动端  │
├──────────────────────────────────────────────────────────────┤
│  应用层  事件服务 · 派单引擎 · 统计服务 · 报表服务 · 用户权限  │
├──────────────────────────────────────────────────────────────┤
│  智能层  AI 推理服务（检测/分类）· 时序校验 · 误报过滤         │
├──────────────────────────────────────────────────────────────┤
│  数据层  PostgreSQL(POSTGIS) · Redis · MinIO(对象存储) · MQTT  │
├──────────────────────────────────────────────────────────────┤
│  接入层  RTSP 流接入 · 边缘盒上报 · 机器人 MQTT 上行 · 无人机  │
└──────────────────────────────────────────────────────────────┘
```

### 五个软件子系统

| 子系统 | 位置 | 责任组 | 代码位置 |
| --- | --- | --- | --- |
| 边缘感知软件 | 岸基摄像头旁边缘盒 | 算法组 | `edge/`（`main.py` 真实程序 + `detector/` OpenCV 检测） |
| 机载软件 | 打捞机器人船载单元 | 硬件组 + 算法组 | `edge/robot/` |
| 数据平台 | 服务器 / 云 | 平台组 | `backend/` + `frontend/` |
| 模型训练管线 | 云端 GPU | 算法组 | `ml/` |
| AI 推理服务 | 与平台同机 | 算法组 + 平台组 | `backend/app/services/ai/` |

### 领域资产认知智能体（Nexent 赛题扩展）

知识域是平台上的横向能力，不替换原有事件与工单闭环。机器人离线时仍可完成：

| 能力 | 代码入口 | 可验收结果 |
| --- | --- | --- |
| 多模态资产统一登记 | `backend/app/services/knowledge.py` | 文档、表格、图片、事件、遥测、数据集资产及不可变版本 |
| 动态本体生命周期 | `backend/app/api/v1/knowledge.py` | 候选抽取 → 人工审核 → 发布，发布版本的证据可追溯 |
| 跨文档多跳检索 | `POST /api/v1/knowledge/search` | 返回命中的节点、关系、路径、片段与引用 |
| 决策链路溯源 | `POST /api/v1/knowledge/decisions` | 保存证据顺序、资产版本、本体版本与策略版本 |
| Nexent MCP + Skills | `integrations/nexent/` | 只读工具默认启用；写入工具显式开启；5 个工作流模板可复用 |

接口与部署边界见 `docs/deployment.md` 与 `docs/architecture.md`，MCP 运行说明见
`integrations/nexent/README.md`。

## 三、技术栈

| 层 | 选型 | 说明 |
| --- | --- | --- |
| 后端 | Python 3.11 + FastAPI | 异步、自带 OpenAPI 文档、与算法同语言 |
| 主数据库 | PostgreSQL 14 + PostGIS | 空间索引支撑热力图与就近派单 |
| 缓存/队列 | Redis 7 | 缓存 + Streams 事件队列 |
| 消息总线 | EMQX 5 (MQTT) | 移动网络友好的设备接入 |
| 对象存储 | MinIO | 告警帧与作业影像 |
| 流媒体 | go2rtc | 单文件、零依赖，RTSP → WebRTC/FLV |
| 前端 | Vue 3 + Vite + ECharts | 政务项目生态成熟 |
| 视频播放 | mpegts.js | HTTP-FLV 低延迟播放（flv.js 已停维护） |
| 地图 | 天地图 API | 政务合规友好 |
| 检测（当前） | OpenCV 5（背景建模 + 颜色/形状规则） | **零数据依赖**，纯 CPU，当场可调参，见 `edge/detector/` |
| 检测（并行通道） | YOLO-World 开放词汇零样本 | 演示与对照用，**默认不启用**；零样本、可现场加词，**不承诺精度**（`detector.backend: world`） |
| 双通道对照页 | `/vision-compare` | 定性展示两条通道各自在什么条件下有效（单图 / 连续帧两个 regime），数据由 `make vision-compare-export` 实测导出，**不含精度指标** |
| 检测（备选） | YOLO11 + ONNX Runtime / TensorRT | 数据攒够后可无缝替换（接口一致），管线保留在 `ml/` |
| 容器 | Docker Compose | 单机一键起全套 |

## 四、快速开始

```bash
# 1. 准备环境变量
cp .env.example .env
# 编辑 .env 填入数据库密码等（敏感信息不要提交到仓库）

# 2. 一键启动全部依赖服务
docker compose up -d

# 3. 初始化数据库（建表 + 空间索引 + 种子数据）
make db-init
make migrate-stamp     # ★ 双轨制对齐：标记 baseline 已应用（务必看下方说明）

# 4. 启动后端
make dev-backend

# 5. 启动前端
make dev-frontend

# 6. 跑通演示：模拟边缘盒上报事件 → 平台自动派单
make simulate          # 真实节奏（抽帧 8fps，上报冷却 90s）
make demo              # 演示节奏（冷却压到 3s，几秒就能看到连续告警）

# 6. 跑真实识别（OpenCV 检测，不需要摄像头）
make test-cv-selftest  # 合成海面自检：检测 → 时序 → 报文 全链路
make run-edge-demo     # 合成海面 + 弹窗看检测框
make run-edge          # 真实 RTSP 取流（改 edge/config.yaml 的 source）

打开 <http://localhost:5173> 查看大屏。

# 7. 可选：接入 Nexent（不启动机器人也可以跑）
make nexent-install
cp integrations/nexent/.env.example integrations/nexent/.env
# 在 integrations/nexent/.env 填写专用账号与密码
make nexent-check
make nexent-acceptance
```

本地 Nexent 可通过 stdio 启动 MCP；容器化或远程 Nexent 使用
`http://host.docker.internal:8100/mcp`，并设置
`Authorization: Bearer <SEASIGHT_MCP_SERVER_TOKEN>`。完整边界、生产 Compose
与验收步骤见 `integrations/nexent/README.md`。

**没有 Docker 时**：参考 `docs/deployment.md` 的「本地裸机模式」，用托管 venv 跑后端。
注意此时 `/health` 会如实返回 `degraded`/`down` —— 这是设计行为，不是 bug。

### 生产服务器部署

生产编排入口是 `docker-compose.prod.yml`。它只把前端绑定到宿主机
`127.0.0.1:8080`，PostgreSQL、Redis、MinIO、后端和 AI 服务都不暴露公网；
EMQX 默认只绑定回环地址，真实设备接入时再把 `MQTT_BIND_ADDR` 改成受防火墙或
VPN 保护的设备网卡地址。生产数据库首启只执行 `01_schema.sql`，不会导入演示种子。

```bash
# 1. 准备生产环境变量并替换所有 CHANGE_ME
cp .env.production.example .env.production

# 2. 校验配置，并生成 EMQX 首次启动所需的密码哈希
make prod-config
make prod-mqtt-bootstrap

# 3. 构建并启动
make prod-up

# 4. 新库将 01_schema.sql 对齐到当前 Alembic head
make prod-migrate-stamp

# 5. 创建正式管理员（交互式输入至少 12 位密码）
make prod-create-admin

# 6. 滚动创建 t_track 未来分区
make prod-track-partitions
```

随后把 `deploy/nginx/seasight.conf` 安装到宿主机 Nginx，并把
`server_name`、TLS 证书和真实域名替换成部署值。对公网只开放 80/443；
不要直接放通 `8000`、`1984`、`9000`、`18083`。

### 数据库 schema 是「双轨制」（改表前必读）

| 轨道 | 负责 | 工具 |
| --- | --- | --- |
| A | 初始建表、PostGIS 扩展、分区表、触发器 | `backend/db/init/01_schema.sql`（容器首启自动执行） |
| B | 之后所有增量变更 | Alembic（唯一主配置：`backend/alembic.ini`，纯 ASCII） |

两轨靠空迁移 `20260918_1000_baseline` 对齐，当前 head 是
`20260919_1900_knowledge_assets`。新环境必须 `make migrate-stamp` 打桩，否则第一次
`make migration` 的 autogenerate 会把已存在的表当成缺表，生成一串
`create_table` 然后执行报错。

```bash
make migration m="给 t_event 加 severity 字段"   # 按模型差异生成迁移
make migrate                                    # 应用
make migrate-history                            # 查看历史
```

> `alembic.ini` 必须保持纯 ASCII：Alembic 读 ini 时使用
> `encoding="locale"`，中文 Windows 会按 **cp936(GBK)** 解码，任何中文注释
> 都可能让所有迁移命令直接崩溃。详细根因见 `docs/deployment.md`。

### 验证与测试

```bash
# 纯逻辑测试，不需要任何基础设施（推荐先跑这个）
make test-edge         # 边缘 86 项：时序校验 11 项（单帧不确认、抽帧压噪、
                       # 抑制率界内、「代码默认值 vs 设计文档参数表」一致性守卫）
                       # + 检测器 75 项（cv 16 + world 59，合成帧 + 注入式假 runner，
                       # 不需要摄像头、数据集，也不需要 torch：
                       # 四类目标分类、bbox 越界、静止目标不丢、反光降分、
                       # ROI 生效、类别与 AI 服务/数据集 yaml 三处对账、
                       # 默认值与 config.yaml 对账、检测器→时序校验器接线、
                       # 开放词汇标签穷举映射、world 不复用 cv 的 0.45 门限、
                       # 缺依赖时优雅降级且可观测、cv 通道实现与 world 无关）

# 真实边缘程序自检：合成海面跑通「检测 → 时序 → 报文」全链路
make test-cv-selftest  # 200 帧 / 原始检测 599 / 确认事件 3 / 时序抑制率 76%（该自检口径，非识别精度；两种口径见 §六）

# 全量 pytest（2026-09-22：1061 通过 / 0 跳过 / 0 失败；含 MQTT、Agent、
# 契约、数据完整性和感知口径测试，
# 事件上报响应契约、派单引擎加权与优先级真源、派单收尾三步、
# Redis Streams PEL 回收、静态守卫、健康检查降级语义、
# 两种模型输出格式解析、可选 OpenAI-compatible 规划模型与规则兜底、
# 认证令牌往返/篡改提权被拒/写接口权限接线、
# 报表聚合乡镇归属与真源对账）
make test

# 静态检查：接口契约 + 仓库卫生 + MQTT/枚举契约漂移（不需要基础设施）
make check             # check-api + check-gitignore + check-contract

# 开源前隐私/脱敏检查：只扫 git 已跟踪文件；私有仓库命中属预期，公开前必须退出码 0
make check-public-repo-privacy

# 自证：注入 12 种已知历史缺陷，确认上面的检查真的会红
make check-contract-selftest

# 自证：注入 4 种事件上报契约缺陷，确认对应的测试真的会红
make check-events-selftest

# 自证：注入 5 种派单引擎缺陷，确认对应的测试真的会红
make check-dispatch-selftest

# 自证：注入 7 种派单收尾缺陷，确认对应的测试真的会红
make check-finalize-selftest

# 自证：注入 4 种 Redis PEL 回收缺陷，确认对应的测试真的会红
make check-pel-selftest

# 端到端冒烟（需要先 make up 起基础设施）
make smoke

# 浏览器集成验收（WP-17，E1：真实登录 + 主业务链路 E2E；需前端 5174 与后端 8001 在线）
$env:PLAYWRIGHT_CORE_PATH='<全局 playwright-core 安装路径>'
node scripts/browser_acceptance.mjs

# 受控故障演练（WP-17，E1：复跑健康降级 / ACK 启动恢复 / 孪生闭环确定性用例）
.\.venv-analysis\Scripts\python.exe scripts\fault_acceptance.py

# Agent 真实状态端到端验收（WP-18，E2：真实 HTTP/权限/PostgreSQL/审批/
# 幂等/进程重启只读回放；使用合成事件，脚本只操作自己启动的 8011 后端）
.\.venv-analysis\Scripts\python.exe scripts\agent_real_state_acceptance.py
```

> 浏览器 E2E 与故障演练为 **E1** 集成验收；WP-18 为 **E2** 软件集成验收。
> WP-18 使用合成事件，AgentRuntime 仍在进程内，重启后只验证已完成 run 的只读
> 回放与幂等重放，不声称未完成 run 可续跑。三者都不是真实用户试点、真实海域
> 或真实硬件验证，不构成 E3/E4 证据。

七层验证各守不同的东西：

| 命令 | 守住什么 | 典型能抓到的错 |
| --- | --- | --- |
| `make test-edge` | **算法逻辑 + 参数真源 + 检测器行为 + 双通道隔离** | 抑制率越界（曾出现 140.9%）、抽帧误杀真实目标、`max_misses` 文档写 5 代码写 6、**暗色渔具被背景建模当成阴影剔掉、静止泡沫被学成背景后永不报警**、**开放词汇通道被套上 cv 的 0.45 门限后一条都过不去**、**未映射的提示词静默变成 other**、**cv 通道实现被世界通道的改动污染** |
| `make test` | **仓库全量：契约 + 推理后处理 + 派单引擎 + 认证权限 + edge/ml** | 状态机非法迁移、schema 字段约束、推送负载漏字段、上报响应谎报派单成功、**派单优化静默失效**、`/health` 撒谎报 ok、模型输出格式换一种就崩、**前端伪造角色提权**、**写接口漏挂 require_operator** |
| `make check-api` | **前后端接口一致性** | 前端调了后端不存在的路径（只在点某个页签时才 404） |
| `make check-gitignore` | **仓库卫生** | 运行时状态文件/模型权重/密钥被误提交 |
| `make check-public-repo-privacy` | **开源前隐私** | 本机路径、生产地址、验收证据目录被带入公开仓库 |
| `make check-contract` | **MQTT/枚举契约漂移** | 订阅了却路由不到、状态映射漏一项、类别枚举五处不一致 |
| `make check-data` | **训练数据完整性** | 图片缺标签（训练静默少用样本，指标对不上）、类别索引越界 |
| `make test-cv-selftest` | **真实识别链路** | 检测器和时序校验器没接上、报文契约漂移、合成海面上检不出目标 |
> `stub` 模式让三组能在没有模型时并行开发，代价是端到端测试跑的是
> stub 而不是真实推理。所以 `_postprocess` 曾经只支持一种模型输出格式，
> 换成 EfficientNMS 导出就会抛 `ValueError` —— 而所有测试都是绿的。
> 破法是**用构造的假张量**去覆盖真实形状（见
> `tests/test_ai_postprocess.py` 的 `TestPostprocessNmsBakedFormat`），
> 不能指望端到端。
>
> **更恶劣的一类：整个协议层不被测试覆盖。**
> `make smoke` 只打 HTTP 接口，压根不经过 MQTT。于是这一层长期零覆盖，
> 而它恰好藏着两个致命缺陷 —— `robot/{id}/cmd/ack` 路由分支用总段数
> 做判据（该主题只有 4 段，条件永不成立），以及作业状态映射表漏了
> `done`。两者**都不抛异常、不打日志**，只表现为「大屏上业务不动了」。
> 修完补了 `tests/test_mqtt_layer.py`（76 项），覆盖路由、状态映射、
> topic 解析、报文形状 —— 全部是纯逻辑，不需要 Broker 和数据库。
>
> **最隐蔽的一类：校验工具自己失效。**
> 上面两个缺陷是同一种形状 —— *文档声明了某集合，代码只实现了一部分*。
> 抽样测试抓不到（手写三条用例很可能正好抽到已实现的那几个），
> 只有**穷举比对两个集合**才行。这就是 `check_contract_drift.py` 的存在理由：
> 它从 `docs/` 解析出声明集合、从代码解析出实现集合，逐项对账。
>
> 但写这类脚本时，我们**连续四次**踩到"工具自身静默失效"：
> AST 遍历漏掉 `AsyncFunctionDef`（5 个 async handler 全部没扫到）、
> 路由判据本身写错导致脚本自造假缺陷、把文档主题总表的「设备 → 平台」
> 误读成「平台必须订阅」、以及自证脚本的注入锚点写错导致该项静默跳过。
>
> 所以配了 `make check-contract-selftest`：**注入 12 种已知历史缺陷，
> 逐项确认守卫真的会红**，任何一项未被捕获或锚点失效即返回非 0。
> 一个永远返回「全绿」的检查脚本比没有脚本更糟 —— 它给的是虚假的安全感。
>
> **同一形状的缺陷还会出现在「手工拼装的负载」里。**
> WebSocket 推送的 data 是手工拼的 dict，散落在 4 个推送点。
> `task_update` 契约声明 4 个字段，其中一处只拼了 3 个 ——
> 前端用 `{...data}` 展开后静默拿到 `undefined`。
> 而当前前端只把该数据当"有更新就重拉"的触发器、不直接渲染字段，
> **所以这个缺陷在界面上完全看不出来**。
>
> 破法是 `tests/test_ws_contract.py`：把契约提为常量，
> 再用 **AST 静态扫描**找出每一处推送调用的字面量 dict，逐个校验键集合
> —— 新增推送点会被**自动纳入**，不需要有人记得来补测试。
> 注意 AST 只对字面量有效，走 `model_dump()` 的推送单独校验模型字段覆盖度。
>
> 自证覆盖**两类**守卫：脚本型（`check_contract_drift.py`）与
> 测试型（`test_ws_contract.py`）。测试型同样会因「扫描器失效导致
> 零问题通过」而空转，所以也要注入缺陷验证它。
>
> **同一形状还会以第三种面貌出现：把「本次调用的产物」当成「可事后反查的东西」。**
> `POST /events` 的响应要回报 `task_created` / `task_id`，
> 而实现是派完单后去查「全库最新一条任务」充数 ——
> 而且 `list_tasks` 返回 `tuple[list[Task], int]`，代码**没解包**，
> `if tasks[0]` 拿到的是 list 恒为真值，判断形同虚设。
>
> 后果比前两次更隐蔽：只要库里有过任意一条历史任务，
> 接口就**恒报派单成功**（无可用机器人时也报）；并发上报时
> `task_id` 指向**别人的**任务，前端拿它查 `/tasks/{task_id}`
> 会看到一个与本事件无关的状态 —— **且任何日志里都不会出现**。
>
> 根因不是「忘了解包」，是**语义错位**：`task_id` 是本次调用的产物，
> 只能由生产它的函数回传。改法是把 `_try_dispatch` 的签名
> 从 `-> None` 改为 `-> Task | None`，并把「签名的每个出口都必须
> 显式 return」也用 AST 穷举钉住（漏一个裸 `return` 会让返回类型
> 退化成 `NoneType`，又是一个静默失效）。
>
> 对应守卫 `tests/test_events_ingest_contract.py`（12 项）+ 
> `make check-events-selftest`（注入 4 种缺陷，全部命中）。
> 扫描器是通用的：凡是「仓库方法返回 `tuple[...]` 却被赋给单个变量
> 后又下标访问」的调用点，一律报出，新方法自动纳入覆盖面。
>
> **第四种面貌更隐蔽：缺陷不在「功能坏掉」，而在「优化白算」。**
> 派单引擎第五步「类别匹配加权」的函数收了 `task` 形参却**从未使用**：
> ```python
> def _class_match_weight(self, event, task):
>     return 0 if event.main_class == FOAM else 1   # task 一次都没出现
> ```
> 于是返回值对同一事件的所有候选机器人**完全相同**，
> 排序实际只剩距离一维 —— 文档承诺的「同类别顺路复用」从未生效。
>
> 这类缺陷**没有任何可断言的错误值**：端到端照样通过、接口照样返回、
> 日志照样打 INFO。系统只是"没那么聪明"了。
> **一个没有断言的优化，等同于不存在。**
>
> 破法是**用结构断言代替值断言**：断言形参确实被使用、
> 断言函数体同时读取了「本次事件类别」与「在途任务类别」两侧、
> 断言同一事件配不同机器人状态时权重**必须不同**
> （旧实现恒相同，这条直接钉死退化）。
> 顺带扫出同源的第二处硬编码 `priority = 1 if ... else 3`，
> 提取为 `TaskPriority` 单一真源。
>
> 对应 `tests/test_dispatch_engine.py`（14 项）+
> `tests/test_static_guards.py`（3 项，全工程扫描「收而不用」形参）+
> `make check-dispatch-selftest`（注入 5 种缺陷，全部命中）。
>
> **第五种面貌：不是「没写对」，是「没接线」。**
> 派单其实是**三步**（建任务 → 下发 MQTT → 推送看板），
> 而四个派单入口里只有两个走完了三步：
>
> | 入口 | 建任务 | 下发 MQTT | 推送看板 |
> | --- | --- | --- | --- |
> | HTTP 上报同步派单 | ✓ | ✓ | ✓ |
> | Streams 消费者 | ✓ | ✓ | ✓ |
> | **定时补派** | ✓ | ✗ | ✗ |
> | **补派 API** | ✓ | ✗ | ✗ |
>
> 后两个的表现是：库里**确实有**任务、日志写着「补派 N 个」、
> API 返回 `{"dispatched": N, "task_ids": [...]}` 且 HTTP 200，
> 而机器人**一条指令都没收到**、看板不刷新，任务永远停在 `assigned`。
> **不报错、不抛异常、不打日志。**
>
> 它与前四种的根本区别：前四种是「声明了 N 项、实现了 N-1 项」，
> 这一种是「**实现了 N 步、只接上了 N-2 步**」。
> 代码本身没错，**grep 也搜得到被调用的函数**——错的是没有人调用它。
>
> 修法是把收尾收成 `dispatch.finalize_dispatch(task)` 一个出口，
> 四个入口全部调用它；守卫则**不列举入口名**，而是用 AST 断言
> 「凡是调了 `dispatch_for_event` 的地方，都必须调 `finalize_dispatch`」
> —— 新入口自动纳入覆盖面，不需要有人记得来补测试。
>
> 同一轮还抓到一个更极端的：**`handle_ack_timeout` 零调用点**。
> 它是文档里「五步筛选」的第五步（ACK 超时换车重派），实现完整、
> grep 得到，就是没人调。后果是机器人掉线后任务永久停在 `assigned`，
> 且因为**根本没有代码在跑**，连一条日志都不会有。
> 现已在定时任务中接上，并补了 `reassign_task` 完成「回退 → 重派」闭环。
>
> 对应 `tests/test_dispatch_finalize.py`（21 项）+
> `make check-finalize-selftest`（注入 7 种缺陷，全部命中）。
>
> **同一家族还有一个变体：流程只写了一半。**
> `dispatch_consumer` 在「无可用机器人」时刻意**不 ACK**，
> 把消息留在 PEL 里等重试 —— 意图是对的。但 `xreadgroup(">")`
> **只读新消息**，PEL 里的条目永远不会被返回，于是：
> 这些消息**永远不会被重投**（机器人上线了也没用），
> PEL 只增不减且**不受 `maxlen` 裁剪影响**。
> 功能上被定时补派从数据库兜住了，所以**从外面完全看不出来** ——
> 「留着重试」实际变成了「永久滞留 + 内存缓慢泄漏」。
>
> 这是 Redis Streams 最经典的坑：**不 ACK 就必须有人认领**。
> 修法是在读新消息前先用 `xautoclaim` 认领挂起超 60 秒的条目；
> 顺带把「已被补派处理、但 Redis 里一直没 ACK」的历史欠账也消化掉
> （状态不是 NEW 就直接 ACK）。
> 对应 `tests/test_consumer_pel.py`（12 项）+
> `make check-pel-selftest`（注入 4 种缺陷，全部命中）。

## 五、目录结构

```
seahawk/
├── backend/                 # FastAPI 后端（平台组主战场）
│   ├── app/
│   │   ├── api/v1/          # HTTP 接口层（只做编排）
│   │   ├── core/            # 配置、依赖注入、异常、安全
│   │   ├── db/              # 数据库会话与基类
│   │   ├── models/          # SQLAlchemy ORM（六张核心表）
│   │   ├── schemas/         # Pydantic 出入参
│   │   ├── repositories/    # 数据访问（含 PostGIS 空间查询）
│   │   ├── services/        # 业务逻辑（派单引擎、状态机）
│   │   ├── mqtt/            # MQTT 客户端与消费者
│   │   └── ws/              # WebSocket 连接管理
│   └── tests/               # 单元测试（状态机 / schema / NMS / 健康降级）
├── frontend/                # Vue3 前端（登录页 · 大屏 · 工单看板 · 报表 · 通知铃铛 · 导出）
│   └── src/{api,components,views,stores,utils}   # views 含 LoginView；utils 含 auth.js
├── ml/                      # 算法训练管线
│   ├── configs/             # 数据集 yaml 与训练配置
│   ├── datasets/            # 数据集占位（真实数据不入库，见其中 .gitkeep）
│   └── scripts/             # 训练、导出、数据集体检
├── edge/                    # 边缘感知软件（真实）+ 模拟器
│   ├── main.py              # 真实边缘程序：取流 → 检测 → 时序 → MQTT 上报
│   ├── config.yaml          # 边缘盒配置（检测器参数 / 时序 / 上报 / 点位）
│   ├── detector/            # 检测通道（两条实现同一契约的通道）
│   │   ├── detector.py      #   cv：背景建模 ∪ 颜色通道 → 轮廓 → 规则分类 → 伪置信度
│   │   ├── world_detector.py#   world：YOLO-World 开放词汇零样本（默认不启用）
│   │   ├── label_map.py     #   开放词汇标签 → 四类的穷举映射表
│   │   ├── clip_shim.py     #   CLIP 文本编码器垫片（可选依赖，缺失可降级）
│   │   ├── config.yaml      #   （参数在上级 config.yaml 的 detector 段）
│   │   ├── test_detector.py #   16 项合成帧测试（不需要摄像头/数据集）
│   │   ├── test_world_detector.py # 59 项（注入式假 runner，不需要 torch）
│   │   └── README.md        #   方案对比、调参指南、已知边界
│   └── simulator/           # 无真实设备时驱动演示（自制检测框）
├── deploy/                  # 部署配置（emqx 认证 / go2rtc 流媒体 / nginx 站点）
├── docs/                    # 项目文档
└── scripts/                 # 静态检查与冒烟测试
    ├── smoke_test.py            # 端到端冒烟（需基础设施）
    ├── check_api_contract.py    # 前后端接口一致性校验
    └── check_gitignore.py       # 仓库卫生检查
```

## 六、核心设计原则

**平台是中枢，不是显示屏。** 识别到垃圾不是弹框，而是产生事件 → 触发派单 → 形成可追溯工单。因此平台的核心表是**事件表与工单表**，不是视频表。

**四条数据流**：

| 数据流 | 方向 | 协议 |
| --- | --- | --- |
| 视频流 | 摄像头 → 流媒体 → 前端 | RTSP → WebRTC/FLV |
| 检测事件流 | 边缘盒 → 平台 | MQTT |
| 调度指令流 | 平台 → 机器人 | MQTT |
| 作业回传流 | 机器人 → 平台 | MQTT + 对象存储 |

**识别可以不准，但绝不能是假的。** 检测走 OpenCV 传统视觉（见 `edge/detector/README.md`）：召回不如 YOLO，但**零数据依赖、纯 CPU、阈值当场可调**，误报由时序校验压制。当前有两个已复现的“时序链路误报抑制率”，但它们来自不同输入集，不能相互替代，也不能作为检测精度报出：

| 场景 / 指标 | 分子 / 分母 | 输入集 | 为什么不同 |
| --- | --- | --- | --- |
| **合成海面自检：76.0%** | `(599 条 fed - 144 条 absorbed) / 599 = 455 / 599 = 75.96%` | 200 帧合成海面自检：599 条原始检测，3 个确认事件 | 该合成流几乎全是瞬态噪声，稳定目标吸收的检测较少，时序过滤能消掉更大比例 |
| **模拟器三场景：42%~43%** | `suppressed / fed`；demo 为 `2451 / 5772 = 42.5%` | 模拟器 demo / stress / regression 的混合目标与噪声流 | 稳定目标贡献更多 `absorbed` 检测，噪声占比低于 76% 场景，因此抑制率较低 |

两者都是同一条时序链路的误报抑制指标，定义为“被时序过滤的检测数 / 输入检测数”；不是检测 precision、recall、识别准确率或现场泛化精度。`76.0%` 的来源为 `edge/main.py` 运行期汇总和 `edge/detector/test_metrics.py`；“3 个确认事件”只是输出结果，不在分子中。`42%~43%` 的来源为 `edge/simulator/simulator.py` 的三场景汇总。当前独立测试集仍为空，感知评测结果为 `not_evaluated`。宁可要一个能演示真实画面、评委可以现场调参的朴素方案，也不要一个跑在 stub 上、用 MD5 造假检测框的"完整系统"。

**权限必须来自服务端签名，不能信任前端填的头。** 四类角色（admin / operator / approver / viewer）由登录签发的签名令牌承载，后端从 `Authorization: Bearer` 验签解析；`require_operator` 挂在三个写接口上（创建任务 / 更新状态 / 补派），匿名、approver 与 viewer 只读；`approver` 只能决定 Agent 人工审批。曾经的身份识别读 `X-User`/`X-Role` 请求头——那是前端自己填的，改一下 localStorage 就能把自己提权成 admin，「有权限」其实是「没权限」。相关守卫见 `tests/test_auth_guard.py`。

**报表必须会涨，打捞量必须真实。** `t_report_daily` 由每日聚合任务（`services/report.py`）从事件/工单明细真实 UPSERT，不再是种子数据一灌就静止；完成工单的打捞量由操作员录入，不再用 `1.5 + Math.random()*11` 造假。

**一个易被质疑的工程判断**：视频流不经过边缘盒。边缘盒旁路取流做推理，避免算力被转发挤占。

## 七、关键文档

| 文档 | 内容 |
| --- | --- |
| `docs/architecture.md` | 系统架构与数据流详解 |
| `docs/api.md` | 接口契约（含统一响应结构） |
| `docs/mqtt-topics.md` | MQTT 主题树设计规范 |
| `docs/deployment.md` | 部署指南（Docker / 裸机两模式） |
| `docs/development.md` | 开发规范与协作流程 |
| `docs/software-design.md` | 软件系统详细技术方案（完整版） |
| `docs/decisions.md` | **技术决策记录（ADR）** —— 每条决策的背景/选项/理由/代价 |

## 八、协作规范

- **分支**：每人独立分支，主分支只由负责人合并
- **提交信息**：`feat:` / `fix:` / `docs:` / `refactor:` 前缀
- **文档**：Markdown 存仓库 `docs/`，重要决策写入 `docs/decisions.md`
- **敏感信息**：密码与密钥只放本地 `.env`，**永不提交**（已在 .gitignore 排除）

## 九、软件基线（WP-00，2026-09-19）

第一波工作包 WP-00 建立的软件基线，全部为可复核事实：

### 9.1 Git 基线

仓库此前无 `.git`，已于 2026-09-19 初始化并提交真实基线（未添加远程、未推送）：

```text
1670ce9 baseline: 用户现有状态
（完整哈希 1670ce97918e5f7d8c7a77d73ca84d92a23a3ac6，155 个文件）
```

`.venv-analysis/` 已加入 `.gitignore`（虚拟环境不入库，避免基线被环境文件污染）。

### 9.2 分析环境（WP-00 验证）

- Python 3.12.14，虚拟环境 `.venv-analysis/`（`pip show pillow` → 11.0.0，与 `backend/requirements-ai.txt` 锁定一致）。
- Windows 控制台默认 GBK：建议运行前设 `$env:PYTHONIOENCODING='utf-8'`；
  两个契约脚本已自带 stdout/stderr UTF-8 重配置与子进程 UTF-8 环境，不依赖该变量。

### 9.3 契约脚本

| 脚本 | 作用 | 退出码 |
| --- | --- | --- |
| `scripts/check_contract_drift.py` | MQTT 主题树 / 处理器注册表 / 作业状态映射 / 类别枚举 / 事件必填字段 五组对账 | 0 = 一致；非 0 = 存在漂移 |
| `scripts/selftest_contract_drift.py` | 注入 12 种已知历史缺陷，验证两类守卫真的会红；含字节级注入-还原自证（SHA256） | 0 = 全部命中且工作区干净 |

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv-analysis\Scripts\python.exe scripts\check_contract_drift.py
.\.venv-analysis\Scripts\python.exe scripts\selftest_contract_drift.py
```

自证逐项结果写入 `scripts/selftest_result.txt`（运行产物，已 gitignore）。

### 9.4 测试口径（2026-09-22 复现）

- 全量 `pytest -q`：**1061 passed / 0 skipped / 0 failed / 1 warning**，收敛于 91.63 秒。
- WP-17 浏览器集成验收：**15/15 通过**（真实登录 + 六页真实读取 + CSV + viewer 403 + MQTT 降级；仅 3 个 Agent 场景受控拦截并标注 `mocked`），报告由本机验收流程生成，不随仓库发布。
- WP-17 受控故障演练：**4/4 组、127/127 用例通过**，报告由本机验收流程生成，不随仓库发布。
- WP-18 Agent 真实状态端到端验收：**129/129 通过**（真实 HTTP/登录权限/PostgreSQL/工单事件/WRITE 审批/幂等/重启只读回放；合成事件、进程内 Runtime），报告由本机验收流程生成，不随仓库发布。
- 范围包括 backend、Agent Runtime、契约与数据完整性、edge 指标口径和 ml 评测脚本测试。
- 健康降级测试固定使用无监听端口，工作区有 Redis/PostgreSQL 时也不会再跳过“全挂”断言。
- 剩余 1 条 warning 为 Starlette `TestClient` 对 `anyio.abc.BlockingPortal` 的第三方弃用告警。
- 感知独立测试集仍为空，本机感知指标产物为 `not_evaluated`；不得用测试全绿或 42%~76% 时序链路误报抑制率替代真实检测精度。
