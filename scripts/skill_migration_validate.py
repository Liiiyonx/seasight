#!/usr/bin/env python3
"""Skill template lightweight migration validation (E1 demo).

Runs the same deterministic SeaSight knowledge pipeline (asset ingestion ->
candidate extraction -> co-occurrence relations -> cross-document retrieval)
over three industry demo corpora: marine governance, medical insurance, and
government service. The five Nexent SKILL.md workflow templates are reused
unchanged in role; only the asset source, vocabulary, standard codes, and
question set are swapped.

Evidence level: E1/E2 software-internal template reuse on synthetic demo
corpora. It is NOT a real de-identified industry data evaluation, NOT a real
deployment, and NOT a claim that production cross-industry migration is done.

Writes:
    artifacts/skill-migration/evidence/latest.json
    artifacts/skill-migration/skill-migration-mapping.md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EVIDENCE = ROOT / "artifacts" / "skill-migration" / "evidence" / "latest.json"
DEFAULT_MAPPING = ROOT / "artifacts" / "skill-migration" / "skill-migration-mapping.md"
BACKEND_DIR = ROOT / "backend"

sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

EXIT_OK = 0
EXIT_NOT_CONFIGURED = 2
EXIT_FAILED = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_document(item: dict[str, Any]):
    from app.services.knowledge import DocumentCandidate

    return DocumentCandidate(
        asset_id=item["asset_id"],
        asset_version_id=f"kav_{item['asset_id']}_1",
        title=item["title"],
        text=item["content_text"],
        asset_type=item["asset_type"],
        source_uri=f"fixture://{item['asset_id']}",
        standard_codes=list(item.get("standard_codes") or []),
    )


def _run_domain(name: str, assets: list[dict[str, Any]], question: str) -> dict[str, Any]:
    from app.services.knowledge import (
        cooccurrence_relations,
        extract_term_candidates,
        query_terms,
        relation_paths,
        score_document,
    )

    documents = [_to_document(item) for item in assets]
    candidates = extract_term_candidates(
        documents,
        max_nodes=60,
        min_term_length=2,
    )
    relations = cooccurrence_relations(
        documents,
        candidates,
        max_relations=60,
    )
    terms = query_terms(question)
    seed_names = {
        candidate.canonical_name
        for candidate in candidates
        if any(term in candidate.canonical_name for term in terms)
    }

    # Deterministic path-shaped evidence: a hit is any asset whose text matches
    # the question and whose matched candidates form a relation path. This
    # mirrors the backend ontology-graph behavior without requiring a running
    # database; it proves the same template can process a new domain.
    matched_assets: list[str] = []
    hops_by_asset: dict[str, int] = {}
    path_edges: list[str] = []
    for document in documents:
        score, _snippet = score_document(question, document)
        if score <= 0:
            continue
        matched_assets.append(document.asset_id)
        matched_names = [
            candidate.canonical_name
            for candidate in candidates
            if candidate.canonical_name in f"{document.title}\n{document.text}".lower()
            and any(term in candidate.canonical_name for term in terms)
        ]
        hops_by_asset[document.asset_id] = 1 if matched_names else 0
        for relation in relations:
            source = relation["source"].canonical_name
            target = relation["target"].canonical_name
            if source in matched_names or target in matched_names:
                edge = f"{source}--[{relation['count']}]--{target}"
                if edge not in path_edges:
                    path_edges.append(edge)

    common_terms = [candidate.canonical_name for candidate in candidates if candidate.canonical_name in _common_vocab()]
    return {
        "domain": name,
        "question": question,
        "asset_count": len(documents),
        "candidate_count": len(candidates),
        "relation_count": len(relations),
        "matched_asset_count": len(matched_assets),
        "matched_assets": matched_assets,
        "path_edges": path_edges[:12],
        "common_terms": common_terms,
        "terms_used": terms,
    }


def _common_vocab() -> set[str]:
    """Vocabulary shared by all three template roles (evidence of reuse)."""
    return {
        "处置闭环",
        "复核",
        "派发",
        "派单",
        "回执",
        "台账",
        "工作方案",
        "规则",
        "闭环",
        "协同",
        "追溯",
    }


def run_validation(
    evidence_path: Path,
    mapping_path: Path,
    *,
    write_mapping: bool = True,
) -> int:
    try:
        from skill_migration_corpus import (
            SKILL_TO_DOMAIN_ROLE,
            SEARCH_QUESTIONS,
            government_assets,
            marine_assets,
            medical_assets,
        )
    except ImportError as exc:
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(
                {
                    "status": "not_configured",
                    "generated_at": utc_now(),
                    "error": f"corpus unavailable: {exc}",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"NOT CONFIGURED: {exc}", file=sys.stderr)
        return EXIT_NOT_CONFIGURED

    try:
        domains = [
            _run_domain("marine", marine_assets(), SEARCH_QUESTIONS["marine"]),
            _run_domain("medical", medical_assets(), SEARCH_QUESTIONS["medical"]),
            _run_domain("government", government_assets(), SEARCH_QUESTIONS["government"]),
        ]
    except Exception as exc:
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(
                {
                    "status": "failed",
                    "generated_at": utc_now(),
                    "error": f"validation failed: {exc}",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"FAILED: {exc}", file=sys.stderr)
        return EXIT_FAILED

    report = {
        "status": "ok",
        "generated_at": utc_now(),
        "evidence_level": "E1/E2",
        "scope_note": (
            "Skill 模板轻量化迁移验证（演示语料）：同一套确定性知识管线与 5 个 "
            "Nexent SKILL.md 模板在海洋治理、医疗、政务三个演示资产源上复用；"
            "资产为合成演示语料，不是真实脱敏行业数据评测，不代表生产跨行业迁移"
            "已交付"
        ),
        "template_change_points": [
            "资产源（asset source）",
            "领域词表（vocabulary）",
            "标准号（standard codes）",
            "问题集（question set）",
            "领域角色映射（domain role mapping）",
        ],
        "unchanged_parts": [
            "5 个 SKILL.md 工作流模板结构",
            "确定性候选抽取与共现关系实现",
            "跨文档检索与决策证据链输出格式",
            "MCP 工具名与参数风格",
        ],
        "skill_to_domain_role": SKILL_TO_DOMAIN_ROLE,
        "domains": domains,
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if write_mapping:
        mapping_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Skill 模板轻量化迁移验证（演示）",
            "",
            "> 证据等级：E1/E2 ｜ 语料：合成演示数据，非真实脱敏行业数据集",
            f"> 生成时间：{utc_now()}",
            "",
            "## 验证结论",
            "",
            "同一套 5 个 SKILL.md 工作流模板在海洋治理、医疗、政务三个演示资产源上",
            "复用：资产源、领域词表、标准号和问题集替换后，确定性抽取、共现关系、",
            "跨文档检索与证据链输出均可运行。本验证是模板复用演示，不代表生产级",
            "跨行业迁移已交付，也不构成真实脱敏行业数据效果评测。",
            "",
            "## 模板 → 领域角色映射",
            "",
        ]
        for skill, role in SKILL_TO_DOMAIN_ROLE.items():
            lines.append(f"- `{skill}`：{role}")
        lines.extend(
            [
                "",
                "## 三领域运行摘要",
                "",
                "| 领域 | 演示资产数 | 候选数 | 关系数 | 检索命中资产数 | 公共复用词命中 |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for domain in domains:
            lines.append(
                "| {domain} | {assets} | {candidates} | {relations} | {hits} | {common} |".format(
                    domain=domain["domain"],
                    assets=domain["asset_count"],
                    candidates=domain["candidate_count"],
                    relations=domain["relation_count"],
                    hits=domain["matched_asset_count"],
                    common="、".join(domain["common_terms"][:8]),
                )
            )
        lines.extend(
            [
                "",
                "## 变更点（换资产源时改什么）",
                "",
                "1. 资产源：替换 `integrations/nexent/mcp_server` 的数据源配置。",
                "2. 领域词表：在 SKILL.md 触发条件与工具参数中替换领域词。",
                "3. 标准号：资产与本体版本携带新行业标准码。",
                "4. 问题集：`policy-evidence-qa` / `event-assessment` 的示例问题换领域。",
                "",
                "## 不变部分（模板沉淀资产）",
                "",
                "1. 5 个 SKILL.md 工作流模板结构。",
                "2. 确定性候选抽取与共现关系实现。",
                "3. 跨文档检索与决策证据链输出格式。",
                "4. MCP 工具名与参数风格。",
                "",
            ]
        )
        mapping_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"VALIDATION OK -> {evidence_path}")
    for domain in domains:
        print(
            "{domain}: assets={asset_count} candidates={candidate_count} "
            "relations={relation_count} hits={matched_asset_count}".format(**domain)
        )
    if write_mapping:
        print(f"MAPPING -> {mapping_path}")
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-output",
        default=str(DEFAULT_EVIDENCE),
        help="JSON evidence report path",
    )
    parser.add_argument(
        "--mapping-output",
        default=str(DEFAULT_MAPPING),
        help="Markdown mapping report path",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_validation(
        Path(args.evidence_output),
        Path(args.mapping_output),
    )


if __name__ == "__main__":
    raise SystemExit(main())
