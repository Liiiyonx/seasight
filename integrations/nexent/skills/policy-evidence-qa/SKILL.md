---
name: policy-evidence-qa
description: Answer questions from registered policy, standard, and governance assets with version-level citations. Use when the user asks what rule applies, which policy supports a decision, or what the authoritative source says.
---

# Policy Evidence QA

## Goal

Produce an evidence-bound answer rather than an unsupported model summary.

## Workflow

1. Clarify the jurisdiction, date, industry, and policy topic from the request.
2. Call `knowledge_list_assets` with `asset_type="document"` and the relevant
   `region`, `query`, or standard code filter.
3. Call `knowledge_search` with `hop_depth=2` and the same policy scope.
4. For every candidate answer, call `knowledge_asset_detail` to verify the
   immutable `asset_version_id`, validity dates, and source URI.
5. If a published ontology exists, select it explicitly and repeat retrieval
   with `ontology_version_id`.
6. Return only claims supported by retrieved snippets.

## Stop Conditions

- If no source is retrieved, state that the current asset base has insufficient
  evidence.
- If two valid versions conflict, show both and identify their effective dates.
- Do not infer a legal conclusion that is absent from the cited source.

## Required Output

Return:

- direct answer;
- applicable conditions and exceptions;
- citation list with `asset_id`, `asset_version_id`, title, and source URI;
- evidence gaps and unresolved version conflicts.
