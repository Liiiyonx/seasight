---
name: marine-event-assessment
description: Assess one marine-waste event using event facts, governance assets, and decision evidence. Use when an operator asks whether an event needs handling, what priority it has, or what evidence supports the assessment.
---

# Marine Event Assessment

## Goal

Convert an event record into an auditable assessment without requiring a robot
to be online.

## Workflow

1. Call `event_get` for the exact `event_id`.
2. Call `dashboard_get` for current operational context.
3. Call `knowledge_list_assets` and `knowledge_search` using the event class,
   township, and relevant policy terms.
4. Call `task_list` with the event-related filters available from the event
   context. If a task identifier is known, call `task_get`.
5. If a decision trace is requested and write permission is enabled, call
   `knowledge_create_decision` with the question, assessment summary, evidence
   depth, and policy version.
6. Read the resulting trace with `knowledge_decision_evidence`.

## Decision Rules

- Treat missing event data as a data-quality issue, not as low risk.
- Never claim that a task was created or dispatched unless an API result
  confirms the task identifier.
- Do not trigger physical dispatch unless the user explicitly requests it and
  the Agent run plus approval workflow permits it.

## Required Output

Return event facts, assessment, evidence, existing work-order state, uncertainty,
and the next permissible action.
