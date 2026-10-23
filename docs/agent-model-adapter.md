# WP-11 可选大模型适配层与规则兜底（agent-model-adapter）

> 版本：1.1
> 日期：2026-09-19
> 工作包：WP-11（第三波并行组 A）
> 上位规格：`docs/agent-program-wave3.md` 第 2、3.2、5 节
> 证据等级：**E1/E2** —— 本层是「模型作为可选规划增强」的适配与校验能力，
> 已提供 OpenAI-compatible HTTP 客户端并完成 Runtime 注入，但默认关闭；
> 自动化测试只使用假客户端、不访问公网。真实部署调用取决于外部配置，
> 仍无真实用户/设备/现场/订单证据，不得写成 E3/E4。

## 1. 目标与边界

让大模型成为可选规划增强，而不是控制面的单点依赖：

- 模型不可用、超时、乱输出或越权时，确定性规则模式（现有
  `RulePlanner` + `RulePolicyGuard`）继续运行，原有派单闭环不降级。
- 配置关闭时**完全不调用**模型客户端（默认关闭）。
- Endpoint、模型名和 API Key 属于部署配置；只有显式开启且 Endpoint/模型名
  完整时，Runtime 才注入真实 HTTP 客户端。
- 自动化测试只注入假客户端或假 opener，绝不访问公网。
- 模型输出只作为「待执行提案」，不得当作事实；必须通过严格 Schema、
  工具存在性、角色权限和风险级别校验后才可能进入执行计划。

本模块已完成配置、导出、Runtime 注入和 Agent API 装配。默认不开启模型时，
运行时状态明确返回 `model_available=false`、`rule_mode=true`。

## 2. 实现与接线范围

```text
backend/app/services/agents/model_adapter.py   # 适配层实现
backend/tests/test_agent_model_adapter.py      # 必测场景 + 加固场景
docs/agent-model-adapter.md                    # 本文档
```

已完成接线：

```text
backend/app/services/agents/__init__.py   # 公开接口导出
backend/app/services/agents/runtime.py    # 可选 planner 注入与状态呈现
backend/app/core/config.py                # 默认关闭的部署配置
backend/app/api/v1/agents.py              # Runtime 构造时按配置装配客户端
.env.example                              # 部署配置模板
```

## 3. 冻结公开接口（不可改名）

```text
JsonModelClient      可注入传输协议：只接收已脱敏的结构化请求（dict），
                     返回 JSON 对象或字符串；不绑定厂商。
ModelStepProposal    模型单步提案，只含 4 个字段：
                     tool_name / arguments / decision_summary / confidence。
ModelPlanProposal    规划结果：source="model" 或 source="rule_fallback"，
                     携带 steps（RuleStep 列表，Runtime 可直接消费）、
                     结构化原因与错误码。
ModelAdapterPlanner  接收 任务 / 业务上下文 / 工具摘要 / 角色 / Schema。
ModelAdapterError    适配层异常基类（code 取 MODEL_* 错误码）。
ModelUnavailableError 模型不可用（连接失败 / 服务不可达 / 未配置传输）。
ModelTimeoutError    模型调用超时。
ModelSchemaError     模型输出未通过严格校验（非法 JSON / 缺字段 / 类型错 /
                     超长 / 未知工具 / 未允许参数 / 越权 / 被禁止字段）。
```

扩展辅助：`OpenAICompatibleModelClient`（OpenAI-compatible
`/chat/completions` 传输，默认不由测试调用）、`summarize_tools(registry)`
（把 `ToolRegistry` 压成脱敏工具摘要）、`DEFAULT_MODEL_SCHEMA`
（严格输出 Schema）、`STEP_SCHEMA`、`SOURCE_MODEL` / `SOURCE_RULE_FALLBACK`。

## 4. 关键设计

### 4.1 调用与回退总流程

```text
ModelAdapterPlanner.plan(task, business_context, tool_summary, role, schema)
 ├─ enabled=False（默认）→ 不调用客户端 → rule_fallback(model_disabled)
 ├─ client 为 None     → 不调用客户端 → rule_fallback(model_not_configured)
 ├─ 调用 client.complete(已脱敏 payload)
 │    ├─ ModelTimeoutError / TimeoutError        → rule_fallback(model_timeout)
 │    ├─ ModelUnavailableError / ConnectionError → rule_fallback(model_connection_failed)
 │    ├─ ModelAdapterError（HTTP/传输）          → rule_fallback(保留 exc.code)
 │    └─ 意外异常                                → rule_fallback(model_internal_error)
 ├─ 解析 JSON（str 必须可解析；失败 → model_invalid_json）
 ├─ 严格校验（失败 → 对应 MODEL_* 错误码）
 └─ 通过 → 转化为 RuleStep 列表，返回 source="model"
```

任何回退都产出 `ModelPlanProposal(source="rule_fallback", error_code=…,
fallback_reason=…)`——**明确可观测，绝不假装模型成功**。

### 4.2 严格校验（全部不通过即回退）

1. **字段白名单**：步骤只允许 `tool_name` / `arguments` /
   `decision_summary` / `confidence`；顶层只允许 `plan`。其余字段拒绝。
2. **递归禁止字段**：任何层级出现 `chain_of_thought` / `reasoning`
   （大小写不敏感）→ `model_forbidden_field`。
3. **类型与必填**：4 个字段全必填；`arguments` 必须是对象；
   `confidence` ∈ [0,1]；`tool_name` ≤ 64 字符；`decision_summary` ≤ 200
   字符；`arguments` JSON 序列化 ≤ 1024 字符；计划步数 ≤ 10（可配）。
4. **工具存在性**：`tool_name` 必须命中工具摘要 → `model_unknown_tool`。
5. **角色与风险级别**：角色必须在工具 `allowed_roles` 内，否则
   `model_role_denied`（敏感/设备指令工具越权在此拒绝）；
   工具风险级别必须是冻结四值之一。
6. **参数白名单与工具 Schema**：参数键必须 ⊆ 工具
   `input_schema.properties`，且整体通过工具 `input_schema` 校验 →
   违规 `model_disallowed_argument`。
7. **Runtime 二次把关**：集成后 `AgentRuntime._stage_policy` /
   `_stage_execute` 仍会对计划执行 `RulePolicyGuard` 与审批预检，
   模型参数即使通过本层校验，仍受确定性控制面约束。

### 4.3 回退结果结构（可观测性）

`ModelPlanProposal` 字段：`source`（固定 `rule_fallback`）、`steps`
（`RulePlanner` 产出的规则计划，默认即派单管道 5 步）、`model_steps`
（空）、`decision_summary`（含「规则兜底 N 步（原因 code）」）、
`fallback_reason`（`code: 中文原因`）、`error_code`、`latency_ms`。

模型结果：`source="model"`、`response_hash`（原始响应哈希）、
`confidence`（步骤均值）、`latency_ms`；`is_proposal=True` 表明
模型输出只是待执行提案，不代表事实。

### 4.4 脱敏与日志纪律

- **请求脱敏**：payload 只含任务摘要（objective/role/param_keys/params）、
  业务上下文、工具契约摘要与严格 Schema；递归丢弃
  `api_key` / `authorization` / `token` / `password` / `secret` /
  `private_key` / `credential` / `cookie` / `session_id` /
  `chain_of_thought` / `reasoning` 键；字符串截断、集合与深度限界；
  Schema 视为契约只做键清洗不截深度。
- **日志**：只记录 `source`、步数、耗时、`response_hash`、错误码；
  **绝不记录完整 Prompt 或模型原始回复**（含业务参数值）。
- **不落思维链**：本模块不保存、不落库、不输出模型思维链；只允许
  决策摘要、工具输入摘要与业务理由进入轨迹（与手册 3.7 解密预算一致）。

### 4.5 错误码表（适配层专用，不进入 Runtime 冻结错误码）

| 错误码 | 触发 | 映射异常 |
| --- | --- | --- |
| `model_disabled` | 配置关闭（默认） | — |
| `model_not_configured` | 未注入 client | — |
| `model_timeout` | 模型超时 | `ModelTimeoutError` / `TimeoutError` |
| `model_connection_failed` | 连接失败 | `ModelUnavailableError` / `ConnectionError` |
| `model_http_error` | HTTP/传输错误 | `ModelAdapterError(code=…)` |
| `model_invalid_json` | JSON 解析失败 | `ModelSchemaError(code=…)` |
| `model_schema_error` | 缺字段/类型错/超长/结构错 | `ModelSchemaError` |
| `model_unknown_tool` | 未知工具 | `ModelSchemaError` |
| `model_disallowed_argument` | 参数不在白名单/不满足工具 Schema | `ModelSchemaError` |
| `model_role_denied` | 角色越权（含敏感工具越权） | `ModelSchemaError` |
| `model_forbidden_field` | 含思维链字段 | `ModelSchemaError` |
| `model_internal_error` | 客户端意外异常 | `ModelAdapterError` |

## 5. 测试覆盖（backend/tests/test_agent_model_adapter.py）

8 个必测场景全部覆盖，另含加固场景：

| 必测场景 | 测试 |
| --- | --- |
| 1 合法模型计划通过严格校验 | `TestValidModelPlan`（含 Runtime 闭环消费） |
| 2 非法 JSON 回退规则模式 | `TestInvalidJsonFallback` |
| 3 缺字段或类型错误回退 | `TestMissingFieldOrTypeError` |
| 4 未知工具或越权角色回退 | `TestUnknownToolOrRoleDenied` |
| 5 模型超时回退 | `TestTimeoutFallback` |
| 6 敏感工具越权回退且不执行 | `TestSensitiveToolDenied`（含完整派单闭环，敏感工具 0 次执行） |
| 7 配置关闭完全不调用客户端 | `TestConfigDisabled`（假客户端调用计数 == 0） |
| 8 日志和结果不含思维链字段 | `TestNoChainOfThought`（caplog + 结果递归扫描） |

加固：连接失败 / HTTP 错误 / 意外异常回退、OpenAI-compatible 请求构造与
完整 URL、超时映射、非法响应、参数白名单、工具 Schema 违规、超长字段、
置信度越界、步数上限、自定义规则计划兜底、坏规则计划显式抛错、payload
脱敏、响应哈希稳定性、延迟测量、冻结接口契约守卫（8 个公开名 + 异常层级 +
冻结 dataclass + 步骤字段恰为 4 个）。

验收命令（与 WP-11 规格一致，同时证明未破坏规则模式与派单闭环）：

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv-analysis\Scripts\python.exe -m pytest backend\tests\test_agent_model_adapter.py backend\tests\test_agent_runtime.py -q
```

## 6. 运行证据（2026-09-22 全量复核）

```text
test_agent_model_adapter.py ................... 51 passed
test_agent_runtime.py        ................. 33 passed（既有测试，未改动）
定向合计 84 passed / 0 failed，退出码 0
全量仓库回归 1061 passed / 0 failed / 1 warning，退出码 0
Agent v1/v2 离线评测 23/23 场景通过，evidence_level=E1
```

## 7. 集成结果与配置

### 7.1 导出与 Runtime

- `backend/app/services/agents/__init__.py` 已导出公开接口和
  `OpenAICompatibleModelClient`。
- `AgentRuntime` 已支持 `model_adapter: ModelAdapterPlanner | None` 注入。
- `_build_plan()` 在注入适配器时调用 `plan(...)`；适配器内部按
  `source="model"` 或 `source="rule_fallback"` 返回可观测结果。
- 状态接口按 `model_adapter.enabled and model_adapter.client is not None`
  返回 `model_available` / `rule_mode`。
- 适配器错误码保持在本层，不写入 Runtime 的冻结 `termination_reason` /
  步骤 `error_code`。

### 7.2 配置模板

```dotenv
AGENT_MODEL_ADAPTER_ENABLED=false
AGENT_MODEL_BASE_URL=
AGENT_MODEL_API_KEY=
AGENT_MODEL_NAME=
AGENT_MODEL_ADAPTER_TIMEOUT_MS=5000
AGENT_MODEL_ADAPTER_MAX_STEPS=10
AGENT_MODEL_MAX_OUTPUT_TOKENS=1024
```

`AGENT_MODEL_BASE_URL` 可填写服务根路径、`/v1`，或完整的
`.../chat/completions`。开关开启但 Endpoint/模型名缺失时，系统记录告警并
继续以规则模式运行。

## 8. 风险与假设

- 模型计划已走 `_stage_policy` 的既有策略守卫与审批预检；本模块的角色/
  风险校验是前置防线，不是审批替代。
- 真实模型服务通过 OpenAI-compatible HTTP 客户端接入，仍默认关闭；
  厂商密钥、Endpoint 和网络质量属于部署环境，不能由离线测试替代验收。
- 超时由客户端实现保证并抛 `ModelTimeoutError`；适配器自身不做线程级
  强杀（避免引入并发复杂度），仅统一映射回退。
- `model_internal_error` 会吞掉客户端意外异常的类型细节（日志只记
  `exc_type`），避免异常消息泄露模型原始回复。
- 规则计划配置错误（如缺 `tool` 键）时 `_fallback` 显式抛
  `ModelAdapterError(model_internal_error)`，不让兜底静默空转。

## 9. 边界与证据状态

- 代码侧没有“待接线”事项：客户端、配置、导出、Runtime 注入和评测接口
  均已接通。
- 尚未使用真实外部模型进行现场/试点效果验收；当前自动化证据仍是假模型、
  假时钟和离线评测，不能据此宣称真实模型准确率或生产稳定性。
- 即使部署后完成真实模型调用，也只能证明 E1/E2 软件集成，不自动获得
  真实用户、真实设备、订单或现场验证的 E3/E4 证据。
