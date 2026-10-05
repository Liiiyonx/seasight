# Oceanus Nexent Integration

This directory connects Oceanus to Huawei ModelEngine Nexent through MCP and
reusable Skills. It does not replace the Oceanus backend and does not import
backend database code. The MCP process supports local `stdio` and remote
Streamable HTTP/SSE transports, and always calls the public `/api/v1` contract.

## Architecture

```text
Nexent Agent
  ├── Skills: business workflows and evidence discipline
  └── MCP: Oceanus Domain Cognition MCP
          │  Bearer token + ApiResponse handling
          ▼
Oceanus FastAPI /api/v1
  ├── knowledge assets, ontology, multi-hop search, decisions
  ├── events and work orders
  └── Agent runtime, tools, approvals, audit mirror
```

The robot is an optional execution endpoint. Knowledge registration, ontology
review, retrieval, decision tracing, event assessment, work-order inspection,
and Agent execution audit remain usable without a robot.

## Setup

```powershell
cd <seahawk-repo>
python -m venv .venv-nexent
.\.venv-nexent\Scripts\python.exe -m pip install -r integrations\nexent\mcp_server\requirements.txt
Copy-Item integrations\nexent\.env.example integrations\nexent\.env
```

Edit `integrations/nexent/.env`:

```dotenv
SEASIGHT_API_BASE_URL=http://127.0.0.1:8000/api/v1
SEASIGHT_API_USERNAME=<dedicated least-privilege account>
SEASIGHT_API_PASSWORD=<password>
SEASIGHT_API_REFRESH_SKEW_SECONDS=300
SEASIGHT_MCP_ALLOW_WRITES=false
```

For a long-running deployment, prefer the account fields above. The MCP server
logs in through `/api/v1/auth/login`, caches the short-lived access token,
refreshes it before expiry, and retries once after an HTTP 401. A signed
`SEASIGHT_API_TOKEN` remains supported as a static override for local smoke
tests or externally managed credentials.

There are two independent Bearer tokens:

| Variable | Direction | Purpose |
| --- | --- | --- |
| `SEASIGHT_API_TOKEN` or username/password | MCP to Oceanus | Outbound calls to `/api/v1` |
| `SEASIGHT_MCP_SERVER_TOKEN` | Nexent to MCP | Inbound authentication for HTTP transports |

Do not reuse the two secrets. HTTP transports refuse to start unless the
inbound token is present and at least 32 characters long.

## Run

```powershell
.\.venv-nexent\Scripts\python.exe integrations\nexent\mcp_server\server.py
```

The default transport is `stdio`, suitable for local Nexent MCP registration.
For a remote or containerized Nexent deployment, use Streamable HTTP:

```dotenv
SEASIGHT_MCP_TRANSPORT=streamable-http
SEASIGHT_MCP_HOST=0.0.0.0
SEASIGHT_MCP_PORT=8100
SEASIGHT_MCP_SERVER_TOKEN=<at least 32 random characters>
SEASIGHT_MCP_PUBLIC_URL=https://mcp.example.com/mcp
```

The remote endpoint is `/mcp`. Nexent must send:

```text
Authorization: Bearer <SEASIGHT_MCP_SERVER_TOKEN>
```

If `sse` is selected instead, the endpoint is `/sse`. Keep the public URL and
the actual endpoint path consistent.

Configuration smoke check:

```powershell
.\.venv-nexent\Scripts\python.exe integrations\nexent\mcp_server\server.py --check
```

End-to-end protocol acceptance:

```powershell
make nexent-acceptance
```

This starts an isolated Streamable HTTP server, verifies fail-closed inbound
authentication, initializes an MCP client, lists the 32 tools, validates the
five Skill front matters, and verifies outbound token refresh against a mock
Oceanus API. Add `--live-api` to `scripts/nexent_acceptance.py` to call
`knowledge_list_assets` against a running backend.

## Production Container

`docker-compose.prod.yml` includes an optional `nexent-mcp` service. It joins
the private backend network, calls `http://backend:8000/api/v1`, and binds the
MCP port to host loopback by default:

```text
http://127.0.0.1:8100/mcp
```

If Nexent runs in another container on the same host, register
`http://host.docker.internal:8100/mcp` and keep the host firewall closed. If it
runs on another machine, publish the port only through a firewall or VPN and
put TLS in front of it.

## Security Boundary

- `SEASIGHT_MCP_ALLOW_WRITES=false` is the default. Only read tools are
  registered in this mode.
- Write tools are not merely hidden: they are not registered unless
  `SEASIGHT_MCP_ALLOW_WRITES=true`.
- The backend remains the authority for roles, township scope, ontology
  review, approval, idempotency, and audit.
- `code != 0` from the `ApiResponse` envelope raises an MCP error. A business
  failure is never returned as successful tool data.
- Use a dedicated account with the least privilege required by the Nexent
  agent. Do not use the production administrator account for read-only
  assistants.
- The static outbound token cannot be refreshed by the MCP process. If it
  expires, an operator must rotate the environment variable and restart the
  service; account/password mode avoids that operational failure.

## Read Tools

| Group | Tools |
| --- | --- |
| Assets | `knowledge_list_assets`, `knowledge_asset_detail` |
| Ontology | `knowledge_list_ontology_versions`, `knowledge_ontology_version_detail`, `knowledge_list_ontology_nodes`, `knowledge_list_ontology_relations` |
| Retrieval | `knowledge_search` |
| Decisions | `knowledge_list_decisions`, `knowledge_decision_evidence` |
| Operations | `event_get`, `event_list`, `task_get`, `task_list`, `task_ack_history`, `dashboard_get` |
| Agent audit | `agent_runtime_status`, `agent_list_runs`, `agent_run_detail`, `agent_run_steps`, `agent_list_tools`, `agent_list_approvals` |

## Optional Write Tools

| Group | Tools |
| --- | --- |
| Assets | `knowledge_create_asset`, `knowledge_append_asset_version` |
| Ontology | `knowledge_create_ontology_version`, `knowledge_extract_ontology`, `knowledge_review_node`, `knowledge_review_relation`, `knowledge_publish_ontology` |
| Decisions | `knowledge_create_decision` |
| Agent control | `agent_create_run`, `agent_cancel_run`, `agent_decide_approval` |

## Skills

The `skills/` directory contains five Nexent-compatible workflow templates:

- `policy-evidence-qa`: answer policy questions with citable asset versions.
- `marine-event-assessment`: assess an event and the relevant governance basis.
- `cross-document-decision`: perform ontology-guided multi-hop reasoning.
- `dispatch-work-order-orchestration`: coordinate events, Agent runs, approvals,
  and work-order inspection without hard-coding a robot vendor.
- `decision-trace-audit`: verify that a decision is traceable to evidence and
  execution steps.

Each Skill defines trigger conditions, tool order, stop conditions, and output
requirements. They are intentionally smaller than an Agent prompt so the same
workflow can be reused across industries by replacing the ontology and asset
sources.
