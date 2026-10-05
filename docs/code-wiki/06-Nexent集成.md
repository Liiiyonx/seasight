# 06 · Nexent 集成（integrations/nexent/）

> 把 Oceanus 的知识资产、动态本体、多跳检索、决策证据链、事件与工单能力通过 **MCP (Model Context Protocol)** 暴露给华为 ModelEngine Nexent 等外部智能体框架。**不替换后端**——所有读写走 `/api/v1` 公共 HTTP 契约，不直接导入后端 DB 模型。

## 1. 架构总览

```
Nexent / 外部智能体
   │  MCP 协议（stdio 或 Streamable HTTP /mcp）
   │  入站认证: Authorization: Bearer <SEASIGHT_MCP_SERVER_TOKEN>
   ▼
integrations/nexent/mcp_server/server.py   (FastMCP, 32 tools)
   │  出站认证: 专用账号登录 / 静态 Bearer Token（自动刷新）
   │  HTTP httpx
   ▼
Oceanus backend /api/v1/*                 (保留角色/范围/审批/审计权限)
```

**双传输模式**：
- `stdio`：本地 Nexent 直接拉起进程（`make nexent-mcp`）
- `streamable-http`：容器化 / 远程部署，默认端口 **8100**，路径 `/mcp`（`http://host.docker.internal:8100/mcp`），带 inbound token 校验

**安全边界**（默认保守）：
- 只读工具默认注册；**写入工具仅当 `SEASIGHT_MCP_ALLOW_WRITES=true` 才注册**
- 出站推荐专用 least-privilege 账号（`scripts/create_nexent_service_account.py` 建号）
- 后端保留完整权限体系：`code != 0` 结构化报错，不吞错

## 2. 服务实现（mcp_server/server.py）

依赖极简：`httpx` + `python-dotenv`（requirements.txt 仅 2 项）。

| 组件 | 行号区间（约） | 职责 |
| --- | --- | --- |
| 配置读取 | L49-69 | `SEASIGHT_API_BASE_URL`、认证模式、`SEASIGHT_MCP_ALLOW_WRITES`、超时、传输模式、HTTP token、端口、public URL |
| `OceanusTokenProvider` | L89-243 | 出站认证：静态 token / 用户名密码两种模式；登录、缓存、**到期前自动刷新**、401 失效重试 |
| `_call()` | L250-317 | 统一 API 调用：组装请求、带 token、401 触发刷新、校验 `ApiResponse.code != 0` 抛结构化错误 |
| FastMCP 实例 | L356-363 | 服务名、host/port、`/mcp` 路径、HTTP 模式 inbound token verifier |
| 只读 knowledge 工具 | L371-487 | 资产列表/详情、本体版本/节点/关系、多跳检索、决策列表与证据链 |
| 只读 operational/agent 工具 | L495-624 | 事件、工单、ACK 历史、dashboard、agent runtime/run/steps/tools/approvals |
| 可写工具注册 | L627-892 | `ALLOW_WRITES=true` 时注册：资产创建/版本追加、本体创建/提取/发布、决策创建、agent run 控制、审批 |
| 配置自检 | L895-953 | `--check` 模式：校验传输/认证配置并输出注册工具数与写入权限状态 |

## 3. 工具面（32 个）

| 分组 | 权限 | 工具 |
| --- | --- | --- |
| knowledge 只读 | 默认启用 | 资产列表/详情、本体版本/节点/关系、多跳检索 search、决策列表、决策证据链 |
| operational 只读 | 默认启用 | 事件、工单、ACK 历史、dashboard 概览 |
| agent 只读 | 默认启用 | runtime status、run 列表/详情、steps、工具目录、审批查询 |
| knowledge 写入 | 显式开启 | 资产创建、版本追加、本体创建/候选抽取/发布、决策创建 |
| agent 写入 | 显式开启 | run 发起/取消、审批决定 |

## 4. Skills（5 个可复用工作流模板）

`skills/` 目录提供开箱即用的多步工作流（资产登记 → 本体演化 → 检索 → 决策存证等组合），由 `make nexent-acceptance` 端到端验证（起隔离 HTTP 服务、校验 inbound auth、列出 32 tools、验证 5 Skills 与 token 刷新）。

## 5. 配置（.env.example 关键项）

| 变量 | 说明 |
| --- | --- |
| `SEASIGHT_API_BASE_URL` | 后端地址 |
| `SEASIGHT_MCP_ALLOW_WRITES` | 写入工具开关（默认 false） |
| `SEASIGHT_MCP_TOKEN` / `SEASIGHT_MCP_USERNAME`+`SEASIGHT_MCP_PASSWORD` | 出站认证（二选一；账号模式支持自动刷新） |
| `SEASIGHT_MCP_TRANSPORT` | `stdio` / `streamable-http` |
| `SEASIGHT_MCP_SERVER_TOKEN` | 入站 HTTP Bearer 校验 |
| `SEASIGHT_MCP_HOST` / `SEASIGHT_MCP_PORT` / `SEASIGHT_MCP_PUBLIC_URL` | 监听与对外地址 |

## 6. 运行方式

```bash
make nexent-install        # 建独立 venv (.venv-nexent)
cp integrations/nexent/.env.example integrations/nexent/.env   # 填专用账号
make nexent-check          # 配置与可导入性自检
make nexent-acceptance     # 端到端验收（报告 artifacts/nexent-acceptance/latest.json）
make nexent-mcp            # stdio 启动
# 远程/容器: Dockerfile 暴露 8100，Nexent 侧配 http://host.docker.internal:8100/mcp
#           + Authorization: Bearer <SEASIGHT_MCP_SERVER_TOKEN>
```

知识进化闭环演示（真实后端）：`make knowledge-evolution-demo`（跑通资产→本体→检索→决策证据链，需 `SEASIGHT_API_*` 环境变量与 admin 账号）。

## 7. 相关验收产物

- `artifacts/nexent-acceptance/latest.json`：验收报告
- `docs/competitions/huawei-nexent.md`：接口与部署边界（赛题口径）
- `integrations/nexent/README.md`：完整运行说明
