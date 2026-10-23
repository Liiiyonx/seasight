#!/usr/bin/env python3
"""Ontology candidate extraction efficiency evaluation (E1, deterministic).

Compares the title-weighted phrase + co-occurrence candidate extractor
(``backend/app/services/knowledge.py``) against a naive raw-token frequency
baseline on an internal synthetic governance corpus.

Scope: this is a software-internal deterministic evaluation on demo/synthetic
text. It is NOT a real-user validation, NOT a domain accuracy number, and NOT
an evaluation on real de-identified industry data. It also does not imply any
perception accuracy.

Writes ``artifacts/ontology-eval/latest.json``.

Exit codes:
    0 = evaluation completed and report written
    2 = not_configured (backend module/dependencies unavailable)
    3 = evaluation failed
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "artifacts" / "ontology-eval" / "latest.json"
BACKEND_DIR = ROOT / "backend"

# The evaluation imports the same service module used by the real backend.
sys.path.insert(0, str(BACKEND_DIR))

EXIT_OK = 0
EXIT_NOT_CONFIGURED = 2
EXIT_FAILED = 3

_SPLIT_RE = re.compile(r"[，。；：、,!?！？;:\s/\\|()（）\[\]【】<>《》“”\"'`]+")
_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{1,63}|[\u4e00-\u9fff]{2,32}")
_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "where",
    "policy", "data", "document", "system", "platform", "result",
    "关于", "以及", "进行", "相关", "问题", "情况", "工作", "管理", "一个",
    "我们", "他们", "可以", "需要", "通过", "根据", "对于", "其中", "进行",
    "数据", "文档", "系统", "平台", "结果", "信息", "内容", "方法",
}

GOLD_TERMS = [
    "海漂垃圾", "岸基摄像头", "打捞机器人", "机械臂", "泡沫", "塑料", "渔网",
    "马鼻镇", "黄岐镇", "筱埕镇", "苔菉镇", "安凯镇", "下宫镇",
    "值班研判", "派单", "拾取", "处置闭环", "网绳缠绕", "称重回执", "回收队伍",
]

GOLD_RELATIONS = [
    ("海漂垃圾", "机械臂"),
    ("马鼻镇", "泡沫"),
    ("黄岐镇", "塑料"),
    ("渔网", "网绳缠绕"),
    ("岸基摄像头", "派单"),
    ("打捞机器人", "拾取"),
    ("值班研判", "派单"),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(term: str) -> str:
    return term.strip().strip("._-").lower()


def _build_corpus():
    from app.services.knowledge import DocumentCandidate

    townships = ["马鼻镇", "黄岐镇", "筱埕镇", "苔菉镇", "安凯镇", "下宫镇"]
    docs: list[Any] = []

    def add(asset_id: str, title: str, text: str, asset_type: str = "document",
            standard_codes: list[str] | None = None) -> None:
        docs.append(
            DocumentCandidate(
                asset_id=asset_id,
                asset_version_id=f"kav_{asset_id}_1",
                title=title,
                text=text,
                asset_type=asset_type,
                source_uri=f"fixture://{asset_id}",
                standard_codes=standard_codes or [],
            )
        )

    add(
        "ast_policy_county",
        "连江县海漂垃圾治理工作方案",
        (
            "连江县海漂垃圾治理工作方案明确：岸基摄像头发现泡沫、塑料、渔网等海漂"
            "垃圾后，由值班研判人员复核，再通过派单平台调度打捞机器人或机械臂执行"
            "拾取。重点区域包括马鼻镇、黄岐镇、筱埕镇、苔菉镇、安凯镇和下宫镇。"
            "拾取完成后回传照片与称重数据，形成处置闭环。"
        ),
        standard_codes=["marine-litter", "lianjiang"],
    )
    for index, township in enumerate(townships[:3], start=1):
        add(
            f"ast_policy_t{index}",
            f"{township}海漂垃圾清理考核细则",
            (
                f"{township}海漂垃圾清理考核细则规定：岸基摄像头发现泡沫、塑料、"
                "渔网等海漂垃圾后，值班研判人员应在时限内完成复核并派单；打捞机器人"
                "或机械臂执行拾取后回传照片与称重数据，未形成处置闭环的计为未完成。"
            ),
        )
    for index, township in enumerate(townships[:4], start=1):
        add(
            f"ast_ledger_t{index}",
            f"{township}海漂垃圾月度台账",
            (
                f"{township}海漂垃圾月度台账显示：{township}泡沫聚集次数最多，"
                "塑料和渔网占比高，网绳缠绕风险集中在养殖区。处置资源包括打捞机器人、"
                "岸基机械臂和人工回收队伍；每条任务记录关联摄像头编号、值班审批人、"
                "派单时间和拾取回执。"
            ),
            asset_type="table",
        )
    add(
        "ast_dispatch_1",
        "岸基监测与处置资源调度规范",
        (
            "岸基摄像头对重点岸段实施全天候监测，发现泡沫、塑料、渔网等海漂垃圾后"
            "自动生成事件；值班研判人员复核后形成派单指令。打捞机器人、岸基机械臂与"
            "人工回收队伍按区域协同处置；每条处置记录回传拾取照片、称重数据与审批人，"
            "形成处置闭环。"
        ),
    )
    add(
        "ast_dispatch_2",
        "打捞机器人与机械臂协同派单规则",
        (
            "打捞机器人适合开阔水域，岸基机械臂适合浅滩与养殖区边缘；值班研判人员"
            "依据摄像头编号和网绳缠绕风险决定派单对象。机械臂执行拾取后回传照片与"
            "称重数据，派单记录进入处置闭环，供决策追溯。"
        ),
    )
    for index, township in enumerate(townships[4:], start=1):
        add(
            f"ast_receipt_t{index}",
            f"{township}海漂垃圾巡查称重回执表",
            (
                f"{township}海漂垃圾巡查称重回执表：回收点按泡沫、塑料、渔网分类称重；"
                "网绳缠绕风险集中，需优先调度机械臂。回执关联摄像头编号、派单时间与"
                "处置结果，形成处置闭环。"
            ),
            asset_type="table",
        )
    return docs


def _has_strong_evidence(term: str, documents) -> bool:
    """True when a term appears in a title or in at least two documents."""
    lowered = term.lower()
    in_title = False
    body_hits = 0
    for doc in documents:
        if lowered in doc.title.lower():
            in_title = True
        if lowered in doc.text.lower():
            body_hits += 1
    return in_title or body_hits >= 2


def _baseline_candidates(documents, *, max_nodes: int):
    """Naive raw-token frequency baseline: no title weighting, no n-grams."""
    counts: Counter[str] = Counter()
    for doc in documents:
        for chunk in _SPLIT_RE.split(f"{doc.title} {doc.text}".lower()):
            chunk = chunk.strip()
            if not chunk:
                continue
            if re.fullmatch(r"[\u4e00-\u9fff]+", chunk):
                if len(chunk) >= 2 and chunk not in _STOPWORDS:
                    counts[_normalize(chunk)] += 1
            else:
                for term in _TERM_RE.findall(chunk):
                    normalized = _normalize(term)
                    if len(normalized) >= 2 and normalized not in _STOPWORDS:
                        counts[normalized] += 1
    ranked = counts.most_common(max_nodes)
    return ranked


def _bounded_hit(term: Any, gold: str, *, max_phrase_len: int = 12) -> bool:
    """Short-phrase granularity hit: exact term or a bounded domain phrase."""
    text = str(term).lower()
    target = gold.lower()
    if text == target:
        return True
    return target in text and len(text) <= max_phrase_len


def _gold_term_hits(terms, gold_terms, *, exact_only: bool = False) -> tuple[int, list[str]]:
    hits = [
        g
        for g in gold_terms
        if any(
            t == g.lower() if exact_only else _bounded_hit(t, g)
            for t in terms
        )
    ]
    return len(hits), hits


def _gold_relation_hits(relations, gold_relations) -> tuple[int, list[str]]:
    hits: list[str] = []
    for source, target in gold_relations:
        matched = any(
            (_bounded_hit(rel["source"].canonical_name, source)
             and _bounded_hit(rel["target"].canonical_name, target))
            or (_bounded_hit(rel["source"].canonical_name, target)
                and _bounded_hit(rel["target"].canonical_name, source))
            for rel in relations
        )
        if matched:
            hits.append(f"{source}--{target}")
    return len(hits), hits


def _median_ms(fn, repeats: int = 3) -> tuple[float, list[float]]:
    timings: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        timings.append((time.perf_counter() - started) * 1000.0)
    return round(statistics.median(timings), 3), [round(t, 3) for t in timings]


def _current_metrics(documents, *, max_nodes: int, min_term_length: int) -> dict[str, Any]:
    from app.services.knowledge import cooccurrence_relations, extract_term_candidates

    def run() -> None:
        extract_term_candidates(
            documents,
            max_nodes=10_000,
            min_term_length=min_term_length,
        )

    median_ms, timings = _median_ms(run)
    all_candidates = extract_term_candidates(
        documents,
        max_nodes=10_000,
        min_term_length=min_term_length,
    )
    candidates = all_candidates[:max_nodes]
    relations = cooccurrence_relations(documents, candidates, max_relations=max_nodes)

    candidate_terms = [c.canonical_name for c in candidates]
    high_confidence = [
        c.canonical_name for c in candidates if _has_strong_evidence(c.canonical_name, documents)
    ]
    short_nodes = [c.canonical_name for c in candidates if len(c.canonical_name) <= 12]
    long_fragments = [c.canonical_name for c in candidates if len(c.canonical_name) > 12]
    gold_hits, gold_hit_terms = _gold_term_hits(candidate_terms, GOLD_TERMS)
    exact_hits, exact_hit_terms = _gold_term_hits(candidate_terms, GOLD_TERMS, exact_only=True)
    relation_hits, relation_hit_pairs = _gold_relation_hits(relations, GOLD_RELATIONS)
    recall_at_budget: dict[int, float] = {}
    relation_recall_at_budget: dict[int, float] = {}
    for budget in (20, 50, 100, 200):
        budget_terms = [c.canonical_name for c in all_candidates[:budget]]
        budget_hits, _ = _gold_term_hits(budget_terms, GOLD_TERMS)
        recall_at_budget[budget] = round(budget_hits / len(GOLD_TERMS), 4)
        budget_relations = cooccurrence_relations(
            documents,
            all_candidates[:budget],
            max_relations=budget,
        )
        budget_relation_hits, _ = _gold_relation_hits(budget_relations, GOLD_RELATIONS)
        relation_recall_at_budget[budget] = round(
            budget_relation_hits / len(GOLD_RELATIONS),
            4,
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
                "strong_evidence": _has_strong_evidence(c.canonical_name, documents),
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


def _baseline_metrics(documents, *, max_nodes: int, min_term_length: int) -> dict[str, Any]:
    def run() -> None:
        _baseline_candidates(documents, max_nodes=max_nodes)

    median_ms, timings = _median_ms(run)
    ranked = _baseline_candidates(documents, max_nodes=10_000)
    all_terms = [term for term, _ in ranked]
    top_terms = [term for term, _ in ranked[:max_nodes]]
    gold_hits, gold_hit_terms = _gold_term_hits(top_terms, GOLD_TERMS)
    exact_hits, exact_hit_terms = _gold_term_hits(top_terms, GOLD_TERMS, exact_only=True)
    short_nodes = [t for t in top_terms if len(t) <= 12]
    long_fragments = [t for t in top_terms if len(t) > 12]
    high_confidence = [t for t in top_terms if _has_strong_evidence(t, documents)]
    recall_at_budget: dict[int, float] = {}
    for budget in (20, 50, 100, 200):
        budget_terms = [term for term, _ in ranked[:budget]]
        budget_hits, _ = _gold_term_hits(budget_terms, GOLD_TERMS)
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


def run_eval(output: Path, *, max_nodes: int, min_term_length: int, repeats: int) -> int:
    try:
        documents = _build_corpus()
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
        current = _current_metrics(documents, max_nodes=max_nodes, min_term_length=min_term_length)
        baseline = _baseline_metrics(documents, max_nodes=max_nodes, min_term_length=min_term_length)
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
    report = {
        "status": "ok",
        "generated_at": utc_now(),
        "evidence_level": "E1",
        "scope_note": (
            "软件内部确定性评测：在内部合成/演示语料上对比本体候选抽取实现与朴素基线；"
            "不是真实用户验证、不是领域准确率、不是真实脱敏行业数据评测，也不代表感知精度"
        ),
        "parameters": {
            "max_nodes": max_nodes,
            "min_term_length": min_term_length,
            "timing_repeats": repeats,
            "corpus_documents": len(documents),
        },
        "gold_reference": {
            "terms": GOLD_TERMS,
            "relations": [f"{a}--{b}" for a, b in GOLD_RELATIONS],
            "note": (
                "内部人工标注样例（fixture），仅用于确定性自评，不是行业标准答案；"
                "命中判据为短领域短语粒度（术语完全命中，或术语出现在不超过 12 字"
                "的候选短语内），长句整块不视为有效本体节点"
            ),
        },
        "current_implementation": current,
        "baseline": baseline,
        "comparison": {
            "gold_term_recall_delta": round(
                current["gold_term_recall"] - baseline["gold_term_recall"],
                4,
            ),
            "gold_term_exact_recall_delta": round(
                current["gold_term_exact_recall"] - baseline["gold_term_exact_recall"],
                4,
            ),
            "short_node_ratio_delta": round(
                current["short_node_ratio"] - baseline["short_node_ratio"],
                4,
            ),
            "long_fragment_ratio_delta": round(
                current["long_fragment_ratio"] - baseline["long_fragment_ratio"],
                4,
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
                "标注术语的覆盖率增量；gold_term_exact_recall_delta 表示精确命中术语"
                "的覆盖率增量；short_node_ratio/long_fragment_ratio 用于说明候选是否"
                "保持短领域短语粒度（朴素基线会把整句块当作候选，不能直接作为本体"
                "节点）；high_confidence_ratio_delta 表示可凭标题/多文档证据批量确认的"
                "候选占比增量；bulk_approve_ratio 为当前实现中可直接批量确认的候选"
                "占比；candidate_expansion_ratio 为 n-gram 展开带来的候选面倍数，"
                "不作为人工审核节省宣称；当前实现还输出带来源证据的候选关系，"
                "朴素基线不生成关系"
            ),
        },
        "limitations": [
            "fixture 为 12 份内部合成治理语料，不是真实脱敏行业数据集",
            "内部人工标注样例由项目自定，不是行业标准答案",
            "抽取耗时在同一进程内测量，仅用于相对对比",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
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
        max_nodes=args.max_nodes,
        min_term_length=args.min_term_length,
        repeats=args.repeats,
    )


if __name__ == "__main__":
    raise SystemExit(main())
