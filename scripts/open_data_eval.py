#!/usr/bin/env python3
"""Public open-data ontology extraction evaluation (E1, deterministic).

Uses the same deterministic title-weighted phrase + co-occurrence extractor as
``ontology_eval.py``, but on a corpus built from publicly accessible pages
published by the Ministry of Ecology and Environment of the People's Republic
of China (mee.gov.cn). The corpus and source URLs are recorded in
``artifacts/open-data-eval/corpus/manifest.json``.

Scope: this is a software-internal deterministic evaluation on public open
documents. It is NOT a real de-identified industry dataset, NOT a domain
accuracy number, and does not imply any perception accuracy or production
deployment.

Writes ``artifacts/open-data-eval/latest.json``.

Exit codes:
    0 = evaluation completed and report written
    2 = not_configured (backend module/dependencies unavailable)
    3 = evaluation failed
"""

from __future__ import annotations

import argparse
import json
import statistics
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
DEFAULT_MANIFEST = ROOT / "artifacts" / "open-data-eval" / "corpus" / "manifest.json"
DEFAULT_OUTPUT = ROOT / "artifacts" / "open-data-eval" / "latest.json"
BACKEND_DIR = ROOT / "backend"

sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(ROOT / "scripts"))

import ontology_eval  # noqa: E402

EXIT_OK = 0
EXIT_NOT_CONFIGURED = 2
EXIT_FAILED = 3

# Gold terms are manually selected domain concepts present in the open corpus
# titles/bodies. They are an internal annotation sample for deterministic
# self-evaluation, not an industry-standard answer set.
GOLD_TERMS = [
    "海洋环境保护法",
    "生态环境法典",
    "生态环境损害鉴定评估",
    "技术指南",
    "环境要素",
    "海洋倾倒",
    "海洋倾倒区选划",
    "海洋倾倒区选划技术导则",
    "海洋倾倒在线监控技术规范",
    "海洋倾倒物质评价规范",
    "废弃物海洋倾倒许可证",
    "生态环境部",
    "生态环境部办公厅",
    "海洋生态环境司",
    "国家生态环境标准",
    "许可证核发",
    "意见征集",
    "书面反馈",
    "在线监控设备",
    "非现场监管",
    "数据传输",
    "征求意见稿",
    "编制说明",
    "疏浚物",
    "渔业废料",
    "惰性无机地质材料",
    "北海区",
    "东海区",
    "南海区",
    "流域海域生态环境监督管理局",
    "政务服务大厅",
    "行政许可",
]

GOLD_RELATIONS = [
    ("海洋倾倒物质评价规范", "疏浚物"),
    ("海洋倾倒物质评价规范", "渔业废料"),
    ("海洋倾倒物质评价规范", "惰性无机地质材料"),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_corpus(manifest_path: Path):
    """Load the open-data corpus from the manifest into DocumentCandidate."""
    from app.services.knowledge import DocumentCandidate

    if not manifest_path.exists():
        raise FileNotFoundError(f"missing open-data manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "ok":
        raise ValueError("open-data manifest status is not ok")

    docs: list[Any] = []
    corpus_dir = manifest_path.parent
    for record in manifest["documents"]:
        extracted = corpus_dir / record["extracted_file"]
        if not extracted.exists():
            raise FileNotFoundError(f"missing extracted corpus file: {extracted}")
        text = extracted.read_text(encoding="utf-8")
        docs.append(
            DocumentCandidate(
                asset_id=record["asset_id"],
                asset_version_id=f"kav_{record['asset_id']}_1",
                title=record["title"],
                text=text,
                asset_type=record.get("asset_type", "document"),
                source_uri=record.get("source_url"),
                standard_codes=record.get("standard_codes", []),
            )
        )
    return docs, manifest


def _current_metrics(documents, *, max_nodes: int, min_term_length: int) -> dict[str, Any]:
    from app.services.knowledge import cooccurrence_relations, extract_term_candidates

    def run() -> None:
        extract_term_candidates(
            documents,
            max_nodes=10_000,
            min_term_length=min_term_length,
        )

    median_ms, timings = ontology_eval._median_ms(run)
    all_candidates = extract_term_candidates(
        documents,
        max_nodes=10_000,
        min_term_length=min_term_length,
    )
    candidates = all_candidates[:max_nodes]
    relations = cooccurrence_relations(documents, candidates, max_relations=max_nodes)

    candidate_terms = [c.canonical_name for c in candidates]
    high_confidence = [
        c.canonical_name
        for c in candidates
        if ontology_eval._has_strong_evidence(c.canonical_name, documents)
    ]
    short_nodes = [c.canonical_name for c in candidates if len(c.canonical_name) <= 12]
    long_fragments = [c.canonical_name for c in candidates if len(c.canonical_name) > 12]
    gold_hits, gold_hit_terms = ontology_eval._gold_term_hits(
        candidate_terms, GOLD_TERMS
    )
    exact_hits, exact_hit_terms = ontology_eval._gold_term_hits(
        candidate_terms, GOLD_TERMS, exact_only=True
    )
    relation_hits, relation_hit_pairs = ontology_eval._gold_relation_hits(
        relations, GOLD_RELATIONS
    )
    recall_at_budget: dict[int, float] = {}
    relation_recall_at_budget: dict[int, float] = {}
    for budget in (20, 50, 100, 200):
        budget_terms = [c.canonical_name for c in all_candidates[:budget]]
        budget_hits, _ = ontology_eval._gold_term_hits(budget_terms, GOLD_TERMS)
        recall_at_budget[budget] = round(budget_hits / len(GOLD_TERMS), 4)
        budget_relations = cooccurrence_relations(
            documents,
            all_candidates[:budget],
            max_relations=budget,
        )
        budget_relation_hits, _ = ontology_eval._gold_relation_hits(
            budget_relations, GOLD_RELATIONS
        )
        relation_recall_at_budget[budget] = round(
            budget_relation_hits / len(GOLD_RELATIONS), 4
        )

    return {
        "algorithm": "title_weighted_phrase + cooccurrence",
        "extract_elapsed_ms_median": median_ms,
        "extract_elapsed_ms_runs": timings,
        "candidate_count_before_cap": len(all_candidates),
        "candidate_count_after_cap": len(candidates),
        "relation_count_after_cap": len(relations),
        "short_node_count": len(short_nodes),
        "short_node_ratio": round(len(short_nodes) / len(candidates), 4),
        "long_fragment_count": len(long_fragments),
        "long_fragment_ratio": round(len(long_fragments) / len(candidates), 4),
        "high_confidence_candidate_count": len(high_confidence),
        "high_confidence_candidate_ratio": round(len(high_confidence) / len(candidates), 4),
        "gold_term_hits": gold_hits,
        "gold_term_total": len(GOLD_TERMS),
        "gold_term_recall": round(gold_hits / len(GOLD_TERMS), 4),
        "gold_term_exact_hits": exact_hits,
        "gold_term_exact_recall": round(exact_hits / len(GOLD_TERMS), 4),
        "gold_relation_hits": relation_hits,
        "gold_relation_total": len(GOLD_RELATIONS),
        "gold_relation_recall": round(relation_hits / len(GOLD_RELATIONS), 4),
        "gold_term_recall_at_budget": recall_at_budget,
        "gold_relation_recall_at_budget": relation_recall_at_budget,
        "gold_term_hit_list": gold_hit_terms,
        "gold_term_exact_hit_list": exact_hit_terms,
        "gold_relation_hit_list": relation_hit_pairs,
        "top_candidates": [
            {
                "term": c.term,
                "canonical_name": c.canonical_name,
                "score": round(c.score, 3),
                "strong_evidence": ontology_eval._has_strong_evidence(
                    c.canonical_name, documents
                ),
                "source_asset_id": c.source_asset_id,
                "source_version_id": c.source_version_id,
            }
            for c in candidates
        ],
        "top_relations": [
            {
                "source": rel["source"].canonical_name,
                "target": rel["target"].canonical_name,
                "count": rel["count"],
                "evidence_count": len(rel["evidence"]),
            }
            for rel in relations[:10]
        ],
    }


def _full_candidate_metrics(documents, *, min_term_length: int) -> dict[str, Any]:
    """Metrics over all extracted candidates/relations (no top-N review cap)."""
    from app.services.knowledge import cooccurrence_relations, extract_term_candidates

    candidates = extract_term_candidates(
        documents,
        max_nodes=10_000,
        min_term_length=min_term_length,
    )
    relations = cooccurrence_relations(documents, candidates, max_relations=10_000)
    candidate_terms = [c.canonical_name for c in candidates]
    gold_hits, gold_hit_terms = ontology_eval._gold_term_hits(
        candidate_terms, GOLD_TERMS
    )
    exact_hits, exact_hit_terms = ontology_eval._gold_term_hits(
        candidate_terms, GOLD_TERMS, exact_only=True
    )
    relation_hits, relation_hit_pairs = ontology_eval._gold_relation_hits(
        relations, GOLD_RELATIONS
    )
    return {
        "candidate_count": len(candidates),
        "relation_count": len(relations),
        "gold_term_hits": gold_hits,
        "gold_term_total": len(GOLD_TERMS),
        "gold_term_recall": round(gold_hits / len(GOLD_TERMS), 4),
        "gold_term_exact_hits": exact_hits,
        "gold_term_exact_recall": round(exact_hits / len(GOLD_TERMS), 4),
        "gold_relation_hits": relation_hits,
        "gold_relation_total": len(GOLD_RELATIONS),
        "gold_relation_recall": round(relation_hits / len(GOLD_RELATIONS), 4),
        "gold_term_hit_list": gold_hit_terms,
        "gold_term_exact_hit_list": exact_hit_terms,
        "gold_relation_hit_list": relation_hit_pairs,
    }


def _baseline_metrics(documents, *, max_nodes: int, min_term_length: int) -> dict[str, Any]:
    def run() -> None:
        ontology_eval._baseline_candidates(documents, max_nodes=max_nodes)

    median_ms, timings = ontology_eval._median_ms(run)
    ranked = ontology_eval._baseline_candidates(documents, max_nodes=10_000)
    all_terms = [term for term, _ in ranked]
    top_terms = [term for term, _ in ranked[:max_nodes]]
    gold_hits, gold_hit_terms = ontology_eval._gold_term_hits(top_terms, GOLD_TERMS)
    exact_hits, exact_hit_terms = ontology_eval._gold_term_hits(
        top_terms, GOLD_TERMS, exact_only=True
    )
    short_nodes = [t for t in top_terms if len(t) <= 12]
    long_fragments = [t for t in top_terms if len(t) > 12]
    high_confidence = [t for t in top_terms if ontology_eval._has_strong_evidence(t, documents)]
    recall_at_budget: dict[int, float] = {}
    for budget in (20, 50, 100, 200):
        budget_terms = [term for term, _ in ranked[:budget]]
        budget_hits, _ = ontology_eval._gold_term_hits(budget_terms, GOLD_TERMS)
        recall_at_budget[budget] = round(budget_hits / len(GOLD_TERMS), 4)

    return {
        "algorithm": "naive_raw_token_frequency (no title weighting, no n-grams)",
        "extract_elapsed_ms_median": median_ms,
        "extract_elapsed_ms_runs": timings,
        "candidate_count_before_cap": len(all_terms),
        "candidate_count_after_cap": len(top_terms),
        "short_node_count": len(short_nodes),
        "short_node_ratio": round(len(short_nodes) / len(top_terms), 4),
        "long_fragment_count": len(long_fragments),
        "long_fragment_ratio": round(len(long_fragments) / len(top_terms), 4),
        "high_confidence_candidate_count": len(high_confidence),
        "high_confidence_candidate_ratio": round(len(high_confidence) / len(top_terms), 4),
        "gold_term_hits": gold_hits,
        "gold_term_total": len(GOLD_TERMS),
        "gold_term_recall": round(gold_hits / len(GOLD_TERMS), 4),
        "gold_term_exact_hits": exact_hits,
        "gold_term_exact_recall": round(exact_hits / len(GOLD_TERMS), 4),
        "gold_term_recall_at_budget": recall_at_budget,
        "gold_term_hit_list": gold_hit_terms,
        "gold_term_exact_hit_list": exact_hit_terms,
        "top_candidates": [
            {"term": term, "count": count}
            for term, count in ranked[:max_nodes]
        ],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST),
        help="open-data corpus manifest path",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="JSON report path",
    )
    parser.add_argument("--max-nodes", type=int, default=20, help="top-N cap for review")
    parser.add_argument("--min-term-length", type=int, default=2, help="minimum term length")
    parser.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="timing repeats (median reported)",
    )
    return parser.parse_args(argv)


def run_eval(
    output: Path,
    *,
    manifest: Path,
    max_nodes: int,
    min_term_length: int,
    repeats: int,
) -> int:
    try:
        documents, manifest_data = _build_corpus(manifest)
    except ImportError as exc:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "status": "not_configured",
                    "generated_at": utc_now(),
                    "error": f"backend module unavailable: {exc}",
                    "scope_note": (
                        "未配置后端运行环境，不伪造评测结果；"
                        "需要 .venv 安装 backend 依赖后重跑"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"NOT CONFIGURED: {exc}", file=sys.stderr)
        return EXIT_NOT_CONFIGURED

    try:
        current = _current_metrics(
            documents, max_nodes=max_nodes, min_term_length=min_term_length
        )
        full_candidate = _full_candidate_metrics(
            documents, min_term_length=min_term_length
        )
        baseline = _baseline_metrics(
            documents, max_nodes=max_nodes, min_term_length=min_term_length
        )
    except Exception as exc:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "status": "failed",
                    "generated_at": utc_now(),
                    "error": f"evaluation failed: {exc}",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"FAILED: {exc}", file=sys.stderr)
        return EXIT_FAILED

    expansion_ratio = round(
        current["candidate_count_before_cap"] / baseline["candidate_count_before_cap"],
        4,
    )
    sources = [
        {
            "asset_id": record["asset_id"],
            "title": record["title"],
            "source_url": record["source_url"],
            "publisher": record["publisher"],
            "text_chars": record["text_chars"],
            "content_sha256": record["content_sha256"],
        }
        for record in manifest_data["documents"]
    ]
    report = {
        "status": "ok",
        "generated_at": utc_now(),
        "evidence_level": "E1",
        "scope_note": (
            "软件内部确定性评测：在生态环境部公开开放通知语料上对比本体候选抽取实现"
            "与朴素基线；语料为可公开访问的官方通知，不是真实脱敏行业数据集、不是"
            "领域准确率、不是人工审核节省、也不代表感知精度或生产部署"
        ),
        "parameters": {
            "max_nodes": max_nodes,
            "min_term_length": min_term_length,
            "timing_repeats": repeats,
            "corpus_documents": len(documents),
            "corpus_manifest": str(manifest.relative_to(ROOT)),
        },
        "gold_reference": {
            "terms": GOLD_TERMS,
            "relations": [f"{a}--{b}" for a, b in GOLD_RELATIONS],
            "note": (
                "内部人工标注样例，仅用于确定性自评，不是行业标准答案；命中判据为"
                "短领域短语粒度（术语完全命中，或术语出现在不超过 12 字的候选短语内），"
                "长句整块不视为有效本体节点"
            ),
        },
        "sources": sources,
        "current_implementation": current,
        "full_candidate_implementation": full_candidate,
        "baseline": baseline,
        "comparison": {
            "gold_term_recall_delta": round(
                current["gold_term_recall"] - baseline["gold_term_recall"], 4
            ),
            "gold_term_exact_recall_delta": round(
                current["gold_term_exact_recall"] - baseline["gold_term_exact_recall"],
                4,
            ),
            "short_node_ratio_delta": round(
                current["short_node_ratio"] - baseline["short_node_ratio"], 4
            ),
            "long_fragment_ratio_delta": round(
                current["long_fragment_ratio"] - baseline["long_fragment_ratio"], 4
            ),
            "high_confidence_ratio_delta": round(
                current["high_confidence_candidate_ratio"]
                - baseline["high_confidence_candidate_ratio"],
                4,
            ),
            "bulk_approve_ratio": current["high_confidence_candidate_ratio"],
            "candidate_expansion_ratio": expansion_ratio,
            "interpretation": (
                "gold_term_recall_delta 表示在相同 top-N 上限下，当前实现命中内部人工"
                "标注术语的覆盖率增量；short_node_ratio/long_fragment_ratio 说明候选是否"
                "保持短领域短语粒度；high_confidence_ratio_delta 表示可凭标题/多文档证据"
                "批量确认的候选占比增量；candidate_expansion_ratio 为 n-gram 展开带来的"
                "候选面倍数，不作为人工审核节省宣称；当前实现还输出带来源证据的候选关系，"
                "朴素基线不生成关系"
            ),
        },
        "limitations": [
            "语料为 8 份生态环境部公开通知，不是真实脱敏行业数据集",
            "内部人工标注样例由项目自定，不是行业标准答案",
            "抽取耗时在同一进程内测量，仅用于相对对比",
            "公开通知正文较短，评测覆盖范围为通知级元数据与文本，不覆盖标准全文条款",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"EVAL OK -> {output}")
    print(
        "current gold recall={:.1%} / baseline={:.1%} / "
        "high-confidence ratio={:.1%} / expansion ratio={:.2f}".format(
            current["gold_term_recall"],
            baseline["gold_term_recall"],
            current["high_confidence_candidate_ratio"],
            expansion_ratio,
        )
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_eval(
        Path(args.output),
        manifest=Path(args.manifest),
        max_nodes=args.max_nodes,
        min_term_length=args.min_term_length,
        repeats=args.repeats,
    )


if __name__ == "__main__":
    raise SystemExit(main())
