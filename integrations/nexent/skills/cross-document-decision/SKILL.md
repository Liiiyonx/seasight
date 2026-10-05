---
name: cross-document-decision
description: Perform ontology-guided multi-hop reasoning across documents, tables, images, events, and datasets. Use for questions that require connecting several assets or explaining a decision path.
---

# Cross-Document Decision

## Goal

Build a traceable path from a question to multiple source assets and ontology
relations.

## Workflow

1. Call `knowledge_list_ontology_versions` and prefer a `published` version.
   Use a draft version only for review preparation, never as approved policy.
2. Call `knowledge_search` with the selected `ontology_version_id` and
   `hop_depth=2` or `3`.
3. Inspect returned `path`, `matched_node_ids`, and `matched_relation_ids`; do
   not treat a snippet without a path as a multi-hop result.
4. Open each material asset with `knowledge_asset_detail` and verify the version
   hash, source, and validity.
5. If the ontology is incomplete, return the missing node or relation as a
   candidate for human review instead of inventing the link.
6. If write permission is enabled, create a decision trace and then read its
   evidence chain.

## Evidence Rules

- A decision path must distinguish direct retrieval from inferred relation
  traversal.
- Rejected ontology nodes and relations cannot support an approved conclusion.
- Do not merge similarly named entities unless the ontology canonical name or
  source evidence supports the merge.

## Required Output

Return the answer, hop-by-hop path, source versions, confidence-lowering gaps,
and any ontology updates needed for a later review cycle.
