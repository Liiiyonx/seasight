---
name: dispatch-work-order-orchestration
description: Orchestrate event assessment, Oceanus Agent execution, human approval, and work-order verification while keeping the execution endpoint replaceable. Use for dispatch or remediation workflows.
---

# Dispatch Work Order Orchestration

## Goal

Coordinate the software decision chain without coupling the workflow to one
robot vendor.

## Preconditions

- Confirm that the user explicitly requests an operational action.
- Require `SEASIGHT_MCP_ALLOW_WRITES=true` and an operator/admin token before
  creating a run.
- Approvals require an admin or approver token.

## Workflow

1. Call `event_get` and verify that the event is actionable.
2. Call `agent_runtime_status` and `agent_list_tools` to confirm the runtime and
   registered tool contract.
3. Call `task_list` to detect an existing active work order.
4. If no active order exists and the user requested action, call
   `agent_create_run` with a stable `idempotency_key`.
5. Call `agent_run_detail` and `agent_run_steps` until the run reaches a
   terminal or `waiting_approval` state.
6. For `waiting_approval`, call `agent_list_approvals`. Do not auto-approve.
   An authorized human must decide through `agent_decide_approval`.
7. Verify the resulting work order with `task_get`; use `task_ack_history` only
   for acknowledged command history.

## Safety Rules

- An empty event list or runtime status is not evidence that a dispatch
  succeeded.
- Never fabricate a robot identifier, work-order identifier, ACK, or completion
  weight.
- Keep knowledge and policy evaluation usable if no execution endpoint is
  available; report the blocked execution step instead of manufacturing one.

## Required Output

Return the event, run, approval, task, execution state, evidence, and any
blocked step with its exact error code.
