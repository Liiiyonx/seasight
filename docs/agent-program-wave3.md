# 探海灵眸第三波 Agent 与证据工程规格

> 版本：1.0
> 日期：2026-09-19
> 上位计划：`项目文档/探海灵眸_项目计划书.md`
> 执行约束：`项目文档/Harness_分工执行手册.md`
> 目标：把 E1 级确定性原型推进为可持久化、可接模型、可故障演练、可接真实数据与设备、可审计外部证据的工程基线。

## 0. 总目标与边界

第三波不是继续堆页面，而是补齐五条真正影响比赛评审的能力：

1. Agent 重启恢复、并发幂等和数据库仓储。
2. 可选大模型适配层，模型不可用时确定性规则兜底。
3. 更严格的评测、故障注入、轨迹回放和多角色协作。
4. 真实感知数据接入、标注、标定和评测工具。
5. 作业设备数字孪生、硬件接口和故障闭环。
6. 真实用户、询价、政策和准现场证据的门禁与台账。

第三波完成后，代码侧最多达到 E1/E2；没有真实用户、真实设备、现场记录、报价、合同、订单或回款，不得写成 E3/E4。

## 1. 全局硬约束

- 工作目录固定为仓库根目录；下文相对路径均以仓库根为基准。
- 当前工作树包含大量未提交改动；不得回滚、清理或覆盖任何既有及用户改动。
- 禁止 `git reset --hard`、`git checkout --`、添加远程或推送。
- 子代理只能修改工作包列出的文件；共享注册文件、主迁移链和 Runtime 接线由总控串行完成。
- 并行子代理不得编辑同一文件。
- 不得保存模型私有思维链；只允许保存决策摘要、工具输入/输出摘要和业务理由。
- 不得把代码、合成数据、模拟设备或规划写成真实海域、真实客户、订单、收入或硬件完成度。
- 未取得真实数据前，感知指标必须保持 `not_evaluated` 或 `null`。
- 无可靠来源时，`coverage_area` 必须为 `NULL`，CSV 留空，禁止写 0。
- `76%` 只能表述为时序链路误报抑制率，不是识别精度、召回率或准确率。
- 所有测试命令必须保留原始退出码和输出尾部。
- 测试不得依赖公网、公网大模型或真实设备；默认使用假时钟、确定性 ID、内存或本地数据库。
- Windows 控制台统一设置 `$env:PYTHONIOENCODING='utf-8'`。

## 2. 波次与依赖

### 并行组 A

```text
WP-10  持久化 Runtime 与重启恢复
WP-11  可选大模型适配层与规则兜底
WP-13  感知数据接入、标注与标定工具
WP-14  作业设备数字孪生与故障注入
WP-15  证据门禁、宣称扫描与材料检查
```

### 并行组 B

```text
WP-12  Agent 评测 v2、故障注入与轨迹回放
```

WP-12 必须在 WP-10 和 WP-11 的公开接口冻结后启动。WP-12 可以读取新接口，但不得修改 WP-10、WP-11 的文件。

### 串行集成

```text
WP-16  总控集成与全量回归
```

WP-16 只由总控执行，负责共享文件、数据库主迁移链、API 工厂、Runtime 依赖注入、全量测试、契约检查、Agent 评测和前端构建。

## 3. 冻结接口

### 3.1 持久化仓储

WP-10 新增的公开类名固定为：

```text
SqlAlchemyRunRepository
SqlAlchemyApprovalRepository
AgentRunState
```

仓储必须实现现有 `RunRepository` 和 `ApprovalRepository` 协议，不得修改协议语义。

`AgentRunState` 只新增一张表 `t_agent_run_state`：

```text
id
run_id
request_json
runtime_state_json
idempotency_key
state_version
created_at
updated_at
```

约束：

```text
run_id 唯一
idempotency_key 建唯一部分索引，NULL 不参与唯一
(run_id, state_version) 用于乐观并发检查
```

`request_json` 与 `runtime_state_json` 只存续跑所需的业务摘要和工具绑定，禁止保存模型思维链、密钥、原始 Prompt 或未经脱敏的敏感载荷。

仓储必须满足：

- 相同幂等键并发创建时最多产生一个 run。
- 同一 run 的步骤仍通过 `t_agent_step` 保留 `(run_id, step_no)` 唯一约束。
- 保存冲突必须抛出现有 `TaskConflictError`，不得静默覆盖。
- 新仓储实例能从数据库读取未完成 run，并允许 `AgentRuntime.resume()` 继续。
- 内存仓储保持可用，数据库不可用时不得破坏离线测试。

### 3.2 模型适配层

WP-11 新增的公开接口固定为：

```text
JsonModelClient
ModelStepProposal
ModelPlanProposal
ModelAdapterPlanner
ModelAdapterError
ModelUnavailableError
ModelTimeoutError
ModelSchemaError
```

`JsonModelClient` 是可注入传输协议，只接收已脱敏的结构化请求，返回 JSON 对象或字符串。测试必须注入假客户端，不得访问公网。

`ModelAdapterPlanner` 规则：

- 只允许模型提出已注册工具和白名单参数。
- 只保存 `decision_summary`，不得保存 Chain-of-Thought。
- 模型输出必须通过严格 Schema、工具存在性、角色权限和风险级别校验。
- 超时、非法 JSON、Schema 错误、未知工具或敏感工具越权时，回退到现有 `RulePlanner`。
- 回退必须是可观测的明确结果，不得假装模型成功。
- 模型不可用时，原有派单闭环必须继续工作。

### 3.3 评测 v2

WP-12 新增场景至少覆盖：

```text
persistent_restart_resume
concurrent_idempotent_trigger
stale_state_conflict
model_valid_plan
model_invalid_json_fallback
model_timeout_fallback
model_sensitive_tool_denied
trace_replay_integrity
multi_role_handoff
device_command_fault_injection
```

新增聚合指标至少覆盖：

```text
restart_recovery_rate
idempotency_conflict_rate
model_fallback_rate
model_schema_rejection_rate
trace_replay_match_rate
approval_handoff_success_rate
device_fault_recovery_rate
```

所有指标必须有定义、分子、分母和 `None` 语义；分母为零时输出 `null`。

### 3.4 感知数据协议

WP-13 新增的标准数据交换格式固定为：

```text
Oceanus COCO-like JSON
```

最小顶层字段：

```text
schema_version
dataset_id
dataset_type
evidence_level
source
captured_at
camera
images
annotations
categories
calibration
checksums
```

标注对象必须包含：

```text
image_id
category_id
bbox
area
iscrowd
source_type
review_status
```

`source_type` 固定为：

```text
manual
imported
synthetic
quasi_real
real
```

其中 `synthetic` 最高只能标 E2；`real` 默认 E3，但必须带真实采集来源和复核状态。

### 3.5 设备数字孪生

WP-14 新增的设备命令固定为：

```text
dispatch
pause
resume
return_home
emergency_stop
ack
```

遥测最小字段固定为：

```text
device_id
seq
timestamp
mode
battery
position
velocity
bin_usage
mission_id
fault_code
```

故障码至少覆盖：

```text
ack_timeout
duplicate_ack
out_of_order_ack
battery_critical
bin_full
gps_lost
communication_lost
emergency_stop
```

设备孪生测试只能标记 E1/E2。真实设备接入、现场联调和连续运行必须另立证据条目。

## 4. WP-10 持久化 Runtime 与重启恢复

### 目标

把进程内内存 Runtime 升级为“内存默认、数据库可选”的持久化运行候选，支持重启恢复、并发幂等和可审计轨迹。

### 允许新增

```text
backend/app/models/agent_state.py
backend/alembic/versions/20260919_1600_agent_persistent_state.py
backend/app/services/agents/persistent_repository.py
backend/tests/test_agent_persistent_repository.py
```

### 集成请求

以下文件不得直接修改，完成后由总控串行编辑：

```text
backend/app/models/__init__.py
backend/app/services/agents/__init__.py
backend/app/api/v1/agents.py
backend/db/init/01_schema.sql
```

### 实现要求

- `SqlAlchemyRunRepository` 与 `SqlAlchemyApprovalRepository` 实现现有协议。
- 使用同步 SQLAlchemy Engine 和独立 Session，供已有的同步 Runtime 调用。
- 引擎和 Session 工厂必须可注入，测试可使用 scratch PostgreSQL 或 SQLite 内存。
- 所有写操作必须在事务内完成；冲突回滚并保留原数据。
- 创建 run 时同时写入 `t_agent_run`、`t_agent_run_state` 和必要的初始步骤。
- 保存 run 时必须检查 `state_version`，旧版本写入抛 `TaskConflictError`。
- 步骤写入必须保持唯一约束，不得覆盖历史步骤。
- 审批创建、查询、决策必须持久化并能跨进程读取。
- 新增 `close()` 或等价资源释放方法，避免测试泄漏连接。
- 不修改现有内存仓储的默认行为。

### 必测场景

1. 创建 run 后新仓储实例可读取。
2. 等待审批的 run 重启后可通过 `resume()` 继续。
3. 同一幂等键并发触发只创建一个 run。
4. 旧 `state_version` 保存冲突。
5. 重复 `(run_id, step_no)` 写入失败。
6. 审批决策跨仓储实例可见且不可重复覆盖。
7. 数据库异常时事务回滚。
8. 敏感字段脱敏，不出现 `api_key`、`authorization`、`chain_of_thought`、`reasoning`。

### 验收命令

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv-analysis\Scripts\python.exe -m pytest backend\tests\test_agent_persistent_repository.py -q
```

如果真实 PostgreSQL 可用，还必须执行 scratch 库迁移和重启恢复测试；不得操作主库 `seasight`。

## 5. WP-11 可选大模型适配层与规则兜底

### 目标

让大模型成为可选规划增强，而不是控制面的单点依赖。模型不可用、超时、乱输出或越权时，确定性规则模式继续运行。

### 允许新增

```text
backend/app/services/agents/model_adapter.py
backend/tests/test_agent_model_adapter.py
docs/agent-model-adapter.md
```

### 集成请求

```text
backend/app/services/agents/__init__.py
backend/app/services/agents/runtime.py
backend/app/core/config.py
```

上述文件由总控在 WP-11 验收后接线；本项已于 2026-09-19 完成，实际接线、
配置与证据边界见 `docs/agent-model-adapter.md` 第 7、9 节。

### 实现要求

- 定义可注入 `JsonModelClient`，不绑定具体厂商。
- `JsonModelClient` 的任何真实网络实现都必须默认关闭，测试只能使用假客户端。
- `ModelAdapterPlanner` 接收任务、业务上下文、工具摘要、角色和 Schema。
- 输出只允许结构化 `tool_name`、`arguments`、`decision_summary`、`confidence`。
- 拒绝以下模型输出：未知工具、未允许参数、缺字段、非法类型、超长字段、敏感工具越权、包含 `chain_of_thought` 或 `reasoning` 的字段。
- 对超时、连接失败、HTTP 错误和 JSON 解析失败统一回退规则规划。
- 回退结果中必须包含 `source="rule_fallback"` 和结构化原因。
- 模型结果中必须包含 `source="model"`，但不得把模型输出当事实。
- 日志只记录摘要、耗时、哈希和错误码，不记录完整 Prompt 或模型原始回复。

### 必测场景

1. 合法模型计划通过严格校验。
2. 非法 JSON 回退规则模式。
3. 缺字段或类型错误回退。
4. 未知工具或越权角色回退。
5. 模型超时回退。
6. 模型返回敏感工具但角色无权限，回退且不执行。
7. 配置关闭时完全不调用模型客户端。
8. 日志和结果不含思维链字段。

### 验收命令

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv-analysis\Scripts\python.exe -m pytest backend\tests\test_agent_model_adapter.py backend\tests\test_agent_runtime.py -q
```

## 6. WP-12 Agent 评测 v2、故障注入与轨迹回放

### 目标

把第三波新增的持久化、模型回退、并发、角色交接和设备故障纳入固定场景、结构化指标和轨迹回放。

### 允许修改

```text
backend/tests/agent_evals/scenarios.py
backend/tests/agent_evals/metrics.py
backend/tests/agent_evals/contract.py
backend/tests/agent_evals/test_scenario_definitions.py
backend/tests/agent_evals/test_metrics.py
backend/tests/agent_evals/test_latest_schema.py
scripts/run_agent_evals.py
```

### 允许新增

```text
backend/tests/agent_evals/test_wave3_scenarios.py
backend/tests/agent_evals/replay.py
backend/tests/agent_evals/test_replay.py
docs/agent-eval-v2.md
```

### 实现要求

- 保留原 13 个场景的 ID 和语义，不得用新场景替换旧场景。
- 新场景必须引用 WP-10/WP-11 的公开接口，不得 monkey patch 私有属性。
- 评估报告继续包含代码版本、配置哈希、命令、日期、样本量和 evidence level。
- 轨迹回放比较步骤类型、工具、错误码、终态和摘要哈希，不比较原始思维链。
- 新增指标必须提供定义、分子、分母和零分母语义。
- 去重、并发和重启测试必须可在无外网、无真实设备条件下运行。
- 若 WP-10/WP-11 未形成稳定接口，应明确跳过并输出原因，不得伪造通过。

### 验收命令

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv-analysis\Scripts\python.exe -m pytest backend\tests\agent_evals -q
.\.venv-analysis\Scripts\python.exe scripts\run_agent_evals.py
```

## 7. WP-13 感知数据接入、标注与标定工具

### 目标

建立真实视频/图像进入系统前的数据协议、标注转换、完整性校验、标定和评测准备工具。没有样本时只准备工具，不生成虚假指标。

### 允许新增

```text
ml/scripts/convert_annotations.py
ml/scripts/validate_dataset_integrity.py
ml/scripts/calibrate_camera.py
ml/tests/test_annotation_pipeline.py
ml/tests/test_calibration.py
docs/perception-data-protocol.md
```

### 集成请求

```text
ml/scripts/evaluate_opencv.py
ml/scripts/check_dataset.py
ml/configs/seasight.yaml
ml/datasets/manifests/**
```

上述文件不得直接修改，由总控根据工具输出串行集成。

### 实现要求

- 标注转换器至少支持 Oceanus COCO-like JSON，并为 YOLO 文本标注提供明确转换入口。
- 导入时必须校验图片路径、类别、bbox、面积、重复 ID、跨 split 泄漏和校验和。
- 标定工具支持棋盘格内参、畸变参数和简单平面映射；没有图像时必须明确失败，不得给默认假参数。
- 输出报告必须包括输入文件哈希、样本数、类别分布、跳过的坏样本和 evidence level。
- 合成数据最高 E2，真实现场数据不得由脚本自动授予 E4。
- 不得把 OpenCV 写成 YOLO、视觉大模型或已训练深度模型。

### 验收命令

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv-analysis\Scripts\python.exe -m pytest ml\tests\test_annotation_pipeline.py ml\tests\test_calibration.py -q
```

## 8. WP-14 作业设备数字孪生与故障注入

### 目标

建立独立于感知事件模拟器的机器人作业单元数字孪生，验证命令、ACK、遥测、故障和恢复闭环。

### 允许新增

```text
edge/device_sim/__init__.py
edge/device_sim/protocol.py
edge/device_sim/transport.py
edge/device_sim/device.py
edge/device_sim/faults.py
edge/device_sim/test_device.py
edge/device_sim/test_faults.py
docs/device-interface.md
```

### 集成请求

```text
backend/app/services/dispatch.py
backend/app/services/consumer.py
edge/simulator/**
```

### 实现要求

- 传输层必须可注入内存 transport，测试不得依赖真实 MQTT。
- 命令必须带 `command_id`、`device_id`、`seq`、`issued_at`、`expires_at`。
- ACK 必须幂等；重复 ACK、乱序 ACK、超时 ACK 均要有测试。
- 遥测必须包含固定最小字段，并验证电量、仓容、位置时间戳。
- 必须支持断网排队、重连补传和紧急停止优先级。
- 故障注入支持固定随机种子，输出确定性报告。
- 不得声称已接真船、真边缘盒或完成现场验证。

### 验收命令

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv-analysis\Scripts\python.exe -m pytest edge\device_sim -q
```

## 9. WP-15 证据门禁、宣称扫描与材料检查

### 目标

把 E0-E4 纪律变成可执行检查，防止计划书、README、答辩材料和代码注释出现无来源数字、越级宣称或幻觉指标。

### 允许新增

```text
scripts/evidence_registry.py
scripts/check_claims.py
scripts/selftest_evidence_gate.py
docs/evidence-claim-policy.md
docs/evidence-inbox/README.md
docs/evidence-inbox/intake_template.yaml
```

### 集成请求

```text
项目文档/商业证据台账.md
项目文档/探海灵眸_项目计划书.md
README.md
docs/product-readiness.md
```

上述材料文件不得由 WP-15 直接修改，避免触发 DOCX 页码重建；扫描结果交给总控决定。

### 实现要求

- 建立机器可读证据登记表，字段包括 `id`、`claim`、`evidence_level`、`source_type`、`source_ref`、`captured_at`、`review_status`、`owner`。
- 扫描器至少检查：
  - 无来源商业数字。
  - `已成交`、`已签约`、`已回款`、`已部署`等词缺少凭证编号。
  - 合成、模拟、规划被写成真实海域或真实客户。
  - `coverage_area` 在无来源时被写成 0。
  - `76%` 被写成识别精度、召回率或准确率。
  - Agent 内存实现被写成生产级、高可用或分布式。
- 输出 JSON 和人类可读摘要，并提供稳定退出码。
- 扫描器必须允许经过登记的证据豁免，不得仅靠关键词一刀切。
- 自测必须包含故意越级宣称和合法带来源宣称两类夹具。

### 验收命令

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv-analysis\Scripts\python.exe scripts\selftest_evidence_gate.py
.\.venv-analysis\Scripts\python.exe scripts\check_claims.py --root .
```

## 10. WP-16 总控集成与全量回归

只由总控执行。

### 集成职责

- WP-10：更新模型导出、服务导出、初始 SQL、主迁移链和可选仓储工厂。
- WP-11：为 Runtime 增加可注入 planner，默认仍为规则规划；接入配置开关。
- WP-12：把新评测产物写入 `artifacts/agent_evals/latest_v2.json`，保留 v1 结果。
- WP-13：把数据协议接入 manifest 校验和 OpenCV 评测入口。
- WP-14：仅在业务后端有明确适配点时接入，不做未经测试的自动下发。
- WP-15：根据扫描结果修正材料或登记证据，不自动修改计划书和 DOCX。

### 必跑验收

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv-analysis\Scripts\python.exe -m pytest -q -rs
.\.venv-analysis\Scripts\python.exe scripts\check_contract_drift.py
.\.venv-analysis\Scripts\python.exe scripts\selftest_contract_drift.py
.\.venv-analysis\Scripts\python.exe scripts\run_agent_evals.py
.\.venv-analysis\Scripts\python.exe scripts\check_claims.py --root .
```

前端发生改动时追加：

```powershell
npm --prefix frontend run build
node scripts\browser_acceptance.mjs
```

### 完成判定

WP-16 只有在以下条件同时满足时才能标记完成：

- 全量测试无失败。
- 契约漂移检查无警告、无失败。
- 契约自证全部缺陷被捕获。
- Agent v1 与 v2 评测均可复现。
- 模型关闭时规则模式完整通过。
- 数据库仓储在 scratch 库通过重启恢复与并发幂等。
- 证据扫描无未登记的红线宣称。
- 所有新增能力仍正确标注 E1/E2。
- 未取得真实外部证据前，不修改计划书中的 E3/E4 状态。

## 11. 最高价值外部动作

代码侧不能替代真实证据。总控在第三波开发并行期间，团队必须同步争取：

1. 一份乡镇海渔站或海上环卫单位真实访谈记录。
2. 一份硬件 BOM 供应商书面询价。
3. 一份政策文件原文和文号。
4. 一次码头、水槽或半开放环境的准现场验证记录。

这四项是从 E1 向 E3 跃迁的最高价值动作，优先级高于继续增加普通页面功能。
