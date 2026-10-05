---
name: decision-trace-audit
description: Audit whether a decision can be reconstructed from its evidence and Agent steps. Use for traceability review, incident review, or compliance preparation.
---

# Decision Trace Audit

## Goal

Determine whether a recorded conclusion is reproducible from immutable sources
and Agent execution steps.

## Workflow

1. Call `knowledge_list_decisions` and select the target `trace_id`.
2. Call `knowledge_decision_evidence` and sort evidence by `rank_no`.
3. For each evidence row, call `knowledge_asset_detail` using its `asset_id` and
   verify that `asset_version_id` still exists.
4. If the trace references a `run_id`, call `agent_run_detail` and
   `agent_run_steps`.
5. Check the run's policy version, status, tool steps, approvals, and error
   codes against the decision metadata.
6. Report missing evidence, broken references, unapproved ontology support, or
   a decision that was created from model text alone.

## Audit Rules

- A citation string without a resolvable asset version is not sufficient.
- A successful run does not prove that the final business action completed;
  verify the referenced work order separately when one exists.
- Do not modify historical traces. New conclusions must be recorded as new
  traces.

## Required Output

Return audit status, reproducible evidence, broken links, execution-step gaps,
and concrete remediation actions.
