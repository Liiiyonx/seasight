# Agent 评测 v2（WP-12）：故障注入与轨迹回放

> 版本：1.1 · 日期：2026-09-26 · 上位：`docs/agent-program-wave3.md` 第 2、3.3、6 节
> 证据等级：E1/E2（内部实现 / 确定性仿真；无真实用户、设备、现场、报价、合同、订单或回款）
> 1.1 变更：固定场景 23 → 27（多角色研判门禁 + 跨 run 经验闭环），见 2.3 与 3.1。

## 1. 定位

WP-12 在 WP-05 评测系统（v1，13 个固定场景 + 六项指标）之上，把第三波新增能力纳入固定场景、结构化指标与轨迹回放：

- WP-10 持久化 Runtime：重启续跑、并发幂等、乐观锁冲突。
- WP-11 模型适配层：合法计划、非法 JSON / 超时 / 敏感工具越权回退。
- WP-14 设备数字孪生：命令 → ACK → 遥测 → 故障 → 恢复闭环。
- WP-12 自身：轨迹回放完整性、多角色审批交接。

所有场景只使用 WP-10/WP-11/WP-14 的**公开接口**（`SqlAlchemyRunRepository`、
`SqlAlchemyApprovalRepository`、`ModelAdapterPlanner`、`run_device_scenario` 等），
不 monkey patch 任何私有属性。全部离线可复现：SQLite 内存 / 临时文件、
假时钟、假模型客户端、内存 device transport，无公网、无真实模型、无真实设备。

## 2. 场景集（27 个）

### 2.1 WP-05 原 13 个场景（ID 与语义冻结，只读保留）

`normal_dispatch_success`、`no_robot_available`、`policy_denied`、
`approval_rejected`、`approval_timeout`、`approval_approved`、
`tool_timeout`、`max_steps_exceeded`、`invalid_loop_replan`、
`replan_recovery_success`、`rule_mode_without_model`、
`idempotent_replay`、`invalid_tool_output`。

### 2.2 WP-12 新增 10 个场景（只追加，不替换）

| ID | 依赖接口 | 语义 |
| --- | --- | --- |
| `persistent_restart_resume` | WP-10 | 等待审批的 run 落库后，全新仓储实例 + 全新 runtime（模拟重启）`resume()` 续跑并完成派单闭环；第三进程只读校验轨迹完整 |
| `concurrent_idempotent_trigger` | WP-10 | 同一幂等键并发创建最多一个 run（唯一部分索引拒绝）；重复触发 `idempotent_replay=True` 复用 |
| `stale_state_conflict` | WP-10 | 两实例观察同一 `state_version`，其一保存后，旧版本保存必须抛 `TaskConflictError` 且不覆盖原数据 |
| `model_valid_plan` | WP-11 | 假模型返回合法计划 → `source="model"`，步骤可直接被 Runtime 消费并完成闭环 |
| `model_invalid_json_fallback` | WP-11 | 非法 JSON → 回退 `source="rule_fallback"`、`error_code="model_invalid_json"` |
| `model_timeout_fallback` | WP-11 | `ModelTimeoutError` → 回退 `error_code="model_timeout"`，派单闭环不降级 |
| `model_sensitive_tool_denied` | WP-11 | 模型提议 admin-only 敏感工具但角色无权 → `model_role_denied` 回退，handler 执行 0 次 |
| `trace_replay_integrity` | WP-12 replay | 原始轨迹自身回放 100% 匹配；篡改工具名 / 终态的负向控制必须被检出 |
| `multi_role_handoff` | WP-01/03 | operator 申请敏感指令、admin 决策通过后继续执行：跨角色审批交接闭环 |
| `device_command_fault_injection` | WP-14 | dispatch → 注入 battery_critical / gps_lost / communication_lost 并恢复 → 注入 emergency_stop（未恢复）→ 急停后 pause 被拒；固定种子确定性报告 |

### 2.3 第四波新增 4 个场景（只追加，不替换）

本轮把两项新落地的智能体能力纳入固定集：**多角色研判门禁**（「事件研判 Agent」→
「调度执行 Agent」）与**跨 run 经验闭环**（终态复盘 → 同类事件规划期引用）。
冻结清单 `REQUIRED_WAVE4_SCENARIO_IDS` 与 wave1/wave3 分列，互不污染。

| ID | 在测什么 | 一句话判定点 |
| --- | --- | --- |
| `team_assessor_gate_blocks` | 研判门禁真的会拦单 | 研判结论 `recommended_action="manual_review"` → 期望校验终止 run（`failed` / `policy_denied`），调度四工具 handler 执行 **0** 次，计划摘要含「角色分工」，轨迹角色归属只有研判角色 |
| `team_assessor_pass_then_dispatch` | 研判通过后调度照常闭环 | 研判两步 + 调度四步各执行 1 次且全部成功 → `succeeded`；轨迹同时含两类工具调用，角色归属覆盖研判 + 调度两个角色 |
| `lessons_retro_and_reuse` | 经验闭环端到端可用 | 第一次同类事件 `no_robot_available` 失败 → 终态复盘提炼 `(foam, no_robot_standby)`（初始置信度 0.75、引用/确认计数为 0）；第二次同类事件的 plan 摘要出现 `summarize_hits` 的「本次引用经验」标记并引用该条经验，`hit_count=1`、`confirm_count=1`、置信度 0.81 |
| `lessons_scoped_and_evict` | 经验库不会膨胀/串味 | 两个事件类别各自一条 `no_robot_standby`，按 scope + 谓词检索的结果精确等于本类别那条（不串用）；命中/确认分别计数且封顶 99，置信度随证据单调上调并封顶 0.95（永不到 1.0）；成功 run 不硬编新经验 |

**为什么用内核复刻而不是调 API。** 研判「角色」是 API 层的编排概念：API 层的研判依据
来自知识库检索、事件快照与数据库，离线评测里都没有。门禁真正依赖的内核语义只有三条 ——
步骤角色元数据（`RuleStep.role` → `runtime_state["step_roles"]`）、`Expectation` 终止、
工具 handler 是否真实执行。评测因此用**自定义 `rule_plan`（研判两步 + 调度四步，与 API 层
`TEAM_RUN_PLAN` 同结构）+ 自建 stub 工具 + `Expectation(on_violation="terminate",
error_code="policy_denied")`** 复刻这套门禁，并**刻意不 import `app.api.v1.agents`**、
不改 `backend/app/**`。经验场景则直接打开内核既有开关 `RuntimeConfig(lessons_enabled=True)`，
用 `LessonStore.all_lessons()` / `match()` / `summarize_hits` 的**真实返回值**断言，
不硬编期望字符串。

## 3. 指标（13 项 + 1 说明项）

六项 WP-05 指标不变（`success_rate`、`policy_violation_rate`、
`tool_correct_rate`、`invalid_loop_rate`、`recovery_success_rate`、
`p95_decision_latency_ms`）。

WP-12 新增七项聚合指标 —— 每个都有定义、分子、分母与零分母 `null` 语义：

| 指标 | 分子 | 分母 | 零分母 |
| --- | --- | --- | --- |
| `restart_recovery_rate` | 重启后成功续跑的 run 数 | 需要重启续跑的 run 数（新仓储 + 新 runtime 从同一数据库 resume） | null |
| `idempotency_conflict_rate` | 幂等键 / 乐观锁冲突被正确拒绝（`TaskConflictError`）的尝试数 | 冲突写入尝试总数 | null |
| `model_fallback_rate` | 回退 `rule_fallback` 的次数 | 模型规划尝试总次数（每次 `plan()` 记 1，含禁用/超时/非法输出） | null |
| `model_schema_rejection_rate` | 严格校验链拒绝的回退次数（非法 JSON/Schema/未知工具/参数越界/角色越权/禁止字段） | 模型实际返回输出的尝试次数（不含超时/连接类） | null |
| `trace_replay_match_rate` | 回放与原始轨迹匹配的步骤数 | 回放总步骤数（两条轨迹取较长者，缺失/多余计不匹配） | null |
| `approval_handoff_success_rate` | 跨角色审批交接成功次数 | 需要跨角色交接的审批次数 | null |
| `device_fault_recovery_rate` | 成功恢复的注入故障数 | 注入故障总数 | null |

说明项 `business_success_rate` = 业务成功（终态 `succeeded`）run 数 / 已执行场景数。

跳过语义：依赖接口缺失的场景 `skipped=true` 且带 `skip_reason`，**不计入任何分母**（未执行 ≠ 失败），报告注明原因。

### 3.1 口径变化对既有指标的影响（23 → 27 场景）

场景从 23 增到 27，所有比率的分母随之变化。7 项 WP-12 指标（重启恢复 / 幂等冲突 /
模型回退 / Schema 拒绝 / 回放匹配 / 审批交接 / 设备故障恢复）的分子与分母都未变，
数值不动；浮动集中在这几项：

| 指标 | 23 场景 | 27 场景 | 变化原因 |
| --- | --- | --- | --- |
| `success_rate` | 1.0 | 1.0 | 27/27 全部达成期望（含预期失败的负向场景） |
| `policy_violation_rate` | 0.0 | 0.0 | 调度四工具在门禁场景执行 0 次，无违规 |
| `tool_correct_rate` | 1.0 (72/72) | 0.9888 (88/89) | 分母 +17 条 tool_call；其中门禁场景的研判结论步骤按 `policy_denied` 记录，而该码属于「调用正确性违规」集合。这是**刻意与 API 层门禁同口径**（门禁用它表达「结论被否决」），不是新增参数/权限违规 |
| `invalid_loop_rate` | 0.0870 (2/23) | 0.0741 (2/27) | 无效循环 run 仍为 2 个，分母变大 |
| `recovery_success_rate` | 0.3333 (1/3) | 0.3333 (1/3) | 新场景不引入 replan，分子分母都不动 |
| `p95_decision_latency_ms` | 0.0 | 0.0 | 新场景全程假时钟不推进，首条 plan 步骤耗时仍为 0 |
| `business_success_rate` | 0.6522 (15/23) | 0.6296 (17/27) | 新增 2 个成功场景（研判通过、经验键控）与 2 个预期失败场景（门禁拦截、复盘用失败），成功占比略降 |

台账变化：`scenarios` 23 → 27、`tool_calls` 72 → 89、`tool_calls_correct` 72 → 88、
`tool_executions` 71 → 92。**这些数字来自脚本实际重跑，不手工改产物、不为让指标好看而调判定。**

## 4. 轨迹回放（replay.py）

- `compare_traces(recorded, replayed)`：逐位比对步骤指纹（`step_type` / `tool_name` /
  `error_code` / `status` / `decision_summary` 的 sha256 / `input_hash` / `output_hash`），
  外加终态 `terminal_status` 与整体 `trace_hash`。
- **不比较原始思维链**：白名单外的未知键（`chain_of_thought`、`reasoning` 等）一律忽略。
- 步骤数不等时按较长者计分母，缺失/多余步骤计为不匹配。
- `hash_trace` 键序无关（JSON `sort_keys`），确定性可复现。

## 5. 产物与契约

- 脚本输出 `artifacts/agent_evals/latest_v2.json`（schema `2.0`）+ 带时间戳历史
  `eval_*.json`；**v1 产物 `latest.json` 保持原样不覆盖**。
- 报告继续包含：代码版本（git HEAD 短哈希 + 内容指纹）、配置哈希、命令、日期、
  样本量（已执行场景数）、`skipped_count`、evidence level（E1）。
- 契约由 `backend/tests/agent_evals/contract.py` 锁定（`test_latest_schema.py` 校验），
  报告产出前脚本自校验，失败退出码 2。

## 6. 执行与验收

```powershell
$env:PYTHONIOENCODING='utf-8'
.\.venv-analysis\Scripts\python.exe -m pytest backend\tests\agent_evals -q
.\.venv-analysis\Scripts\python.exe scripts\run_agent_evals.py
```

预期：127 个测试全过；脚本退出码 0，27/27 场景通过（当前无跳过）。
若 WP-10/WP-11/WP-14 任一接口缺失，对应场景自动 `skipped` 并在报告中列出原因，不影响退出码 0。

## 7. 纪律声明

本评测全部为 E1（内部实现 / 确定性仿真证据）。模型为注入的假客户端、设备为
内存数字孪生、数据库为 SQLite 内存 / 临时文件 —— 不构成真实模型、真实设备、
真实部署或现场验证证据；不得据此宣称 E3/E4。
