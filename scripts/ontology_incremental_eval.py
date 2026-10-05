#!/usr/bin/env python3
"""Incremental vs full re-extraction evaluation (E1, deterministic).

Compares the deterministic ontology candidate extractor used by the Oceanus
knowledge service in two maintenance modes after one asset version is
appended:

    full         = re-run candidate extraction + co-occurrence over all N+1
                   assets from scratch
    incremental  = run candidate extraction only over the newly appended
                   asset version, merge with already-published candidates,
                   then refresh co-occurrence relations over all documents

The benchmark measures software-internal elapsed time and candidate/relation
change counts on an internal synthetic corpus. It is NOT a real user
validation, NOT a domain accuracy number, and NOT an evaluation on real
de-identified industry data.

Writes ``artifacts/ontology-eval/incremental-latest.json``.

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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "artifacts" / "ontology-eval" / "incremental-latest.json"
BACKEND_DIR = ROOT / "backend"

# The evaluation imports the same service module used by the real backend.
sys.path.insert(0, str(BACKEND_DIR))

EXIT_OK = 0
EXIT_NOT_CONFIGURED = 2
EXIT_FAILED = 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_corpus():
    """Internal synthetic governance corpus with one appended version.

    The appended version intentionally adds a new execution-ends component so
    the incremental mode has a real candidate/relation delta to report while
    still sharing the established policy vocabulary.
    """
    from app.services.knowledge import DocumentCandidate

    docs: list[Any] = []

    def add(
        asset_id: str,
        title: str,
        text: str,
        asset_type: str = "document",
        standard_codes: list[str] | None = None,
    ) -> None:
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
    add(
        "ast_ledger_mabiz",
        "马鼻镇海漂垃圾月度台账",
        (
            "马鼻镇海漂垃圾月度台账显示：泡沫聚集次数最多，塑料和渔网占比高，"
            "网绳缠绕风险集中在养殖区。处置资源包括打捞机器人、岸基机械臂和人工"
            "回收队伍；每条任务记录关联摄像头编号、值班审批人、派单时间和拾取回执。"
        ),
        asset_type="table",
    )
    add(
        "ast_dispatch_1",
        "岸基监测与处置资源调度规范",
        (
            "岸基摄像头对重点岸段实施全天候监测，发现泡沫、塑料、渔网等海漂垃圾后"
            "自动生成事件；值班研判人员复核后形成派单指令。打捞机器人、岸基机械臂"
            "与人工回收队伍按区域协同处置；每条处置记录回传拾取照片、称重数据与"
            "审批人，形成处置闭环。"
        ),
    )

    appended = DocumentCandidate(
        asset_id="ast_dispatch_2",
        asset_version_id="kav_ast_dispatch_2_1",
        title="打捞机器人与岸基机械臂协同派单规则（v2 修订）",
        asset_type="document",
        source_uri="fixture://ast_dispatch_2",
        text=(
            "打捞机器人适合开阔水域，岸基机械臂适合浅滩与养殖区边缘；值班研判人员"
            "依据摄像头编号和网绳缠绕风险决定派单对象。机械臂执行拾取后回传照片与"
            "称重数据，派单记录进入处置闭环。本次修订补充：机械臂末端为可插拔执行"
            "器，浅滩区域优先采用机械臂拾取，防止网绳缠绕导致打捞机器人停机。"
        ),
        standard_codes=["marine-litter", "lianjiang"],
    )
    return docs, appended


def _canonical_terms(candidates: list[Any]) -> list[str]:
    return [item.canonical_name for item in candidates]


def _relation_keys(relations: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        (rel["source"].canonical_name, rel["target"].canonical_name)
        for rel in relations
    ]


def _median_ms(fn, repeats: int) -> tuple[float, list[float]]:
    timings: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        timings.append((time.perf_counter() - started) * 1000.0)
    return round(statistics.median(timings), 3), [round(t, 3) for t in timings]


def run_eval(output: Path, *, max_nodes: int, repeats: int) -> int:
    try:
        base_documents, appended_doc = _build_corpus()
        from app.services.knowledge import cooccurrence_relations, extract_term_candidates
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
        full_documents = list(base_documents) + [appended_doc]

        def run_full() -> None:
            candidates = extract_term_candidates(
                full_documents,
                max_nodes=max_nodes,
                min_term_length=2,
            )
            cooccurrence_relations(
                full_documents,
                candidates,
                max_relations=max_nodes,
            )

        full_ms, full_runs = _median_ms(run_full, repeats)
        full_candidates = extract_term_candidates(
            full_documents,
            max_nodes=max_nodes,
            min_term_length=2,
        )
        full_relations = cooccurrence_relations(
            full_documents,
            full_candidates,
            max_relations=max_nodes,
        )

        published_candidates = extract_term_candidates(
            base_documents,
            max_nodes=max_nodes,
            min_term_length=2,
        )
        published_terms = _canonical_terms(published_candidates)

        def run_incremental() -> None:
            new_candidates = extract_term_candidates(
                [appended_doc],
                max_nodes=max_nodes,
                min_term_length=2,
            )
            merged_terms = list(dict.fromkeys(published_terms + _canonical_terms(new_candidates)))
            # Rebuild candidate objects after the merge so relation generation has
            # source evidence for both the published set and the appended version.
            merged_candidates = list(published_candidates) + [
                item
                for item in new_candidates
                if item.canonical_name not in published_terms
            ]
            cooccurrence_relations(
                full_documents,
                merged_candidates,
                max_relations=max_nodes,
            )

        incremental_ms, incremental_runs = _median_ms(run_incremental, repeats)
        new_candidates = extract_term_candidates(
            [appended_doc],
            max_nodes=max_nodes,
            min_term_length=2,
        )
        new_terms = _canonical_terms(new_candidates)
        merged_terms = list(dict.fromkeys(published_terms + new_terms))
        merged_candidates = list(published_candidates) + [
            item
            for item in new_candidates
            if item.canonical_name not in published_terms
        ]
        incremental_relations = cooccurrence_relations(
            full_documents,
            merged_candidates,
            max_relations=max_nodes,
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

    full_keys = set(_relation_keys(full_relations))
    incremental_keys = set(_relation_keys(incremental_relations))
    appended_delta_terms = [term for term in new_terms if term not in published_terms]
    removed_or_changed_relations = full_keys.symmetric_difference(incremental_keys)

    time_savings_ratio = round(
        (full_ms - incremental_ms) / full_ms if full_ms else 0.0,
        4,
    )
    report = {
        "status": "ok",
        "generated_at": utc_now(),
        "evidence_level": "E1",
        "scope_note": (
            "软件内部确定性评测：在内部合成治理语料上对比全量重抽与增量追加后再抽取"
            "的耗时与候选变化；耗时在同一进程内测量，仅用于相对对比。不是真实用户"
            "验证、不是领域准确率、不是真实脱敏行业数据评测，也不代表感知精度"
        ),
        "parameters": {
            "max_nodes": max_nodes,
            "timing_repeats": repeats,
            "base_assets": len(base_documents),
            "appended_assets": 1,
            "total_assets_after_append": len(full_documents),
            "appended_asset_id": appended_doc.asset_id,
        },
        "maintenance_modes": {
            "full": {
                "label": "全量重抽：所有 N+1 个资产从头抽取候选并生成关系",
                "extract_elapsed_ms_median": full_ms,
                "extract_elapsed_ms_runs": full_runs,
                "candidate_count": len(full_candidates),
                "relation_count": len(full_relations),
            },
            "incremental": {
                "label": "增量追加：只对新增资产版本抽取候选，与已发布候选合并后再刷新关系",
                "extract_elapsed_ms_median": incremental_ms,
                "extract_elapsed_ms_runs": incremental_runs,
                "new_candidate_count": len(new_candidates),
                "merged_candidate_count": len(merged_candidates),
                "relation_count": len(incremental_relations),
                "appended_delta_terms": appended_delta_terms,
            },
        },
        "comparison": {
            "extract_elapsed_ms_delta_full_minus_incremental": round(
                full_ms - incremental_ms,
                3,
            ),
            "time_savings_ratio": time_savings_ratio,
            "candidate_count_delta": len(full_candidates) - len(merged_candidates),
            "relation_count_delta": len(full_relations) - len(incremental_relations),
            "changed_or_removed_relation_count": len(removed_or_changed_relations),
            "changed_or_removed_relations": sorted(
                (f"{a}--{b}" for a, b in removed_or_changed_relations)
            ),
            "interpretation": (
                "增量追加模式复用已发布的候选集合，只对新增版本执行候选抽取，"
                "因此耗时下降（time_savings_ratio）；全量与增量最终都基于同一份"
                "文档全集生成关系，候选/关系差异用于说明增量语义覆盖范围，"
                "不作为领域准确率或人工审核节省宣称"
            ),
        },
        "limitations": [
            "语料为内部合成治理文本，不是真实脱敏行业数据集",
            "全量与增量共享同一确定性抽取实现，差异来自维护模式而非算法准确度",
            "耗时受机器负载影响，仅用于相对对比",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"EVAL OK -> {output}")
    print(
        "full median={} ms / incremental median={} ms / "
        "time savings ratio={:.1%} / appended delta terms={}".format(
            full_ms,
            incremental_ms,
            time_savings_ratio,
            len(appended_delta_terms),
        )
    )
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="JSON report path",
    )
    parser.add_argument("--max-nodes", type=int, default=20, help="top-N cap for review")
    parser.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="timing repeats (median reported)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_eval(
        Path(args.output),
        max_nodes=args.max_nodes,
        repeats=args.repeats,
    )


if __name__ == "__main__":
    raise SystemExit(main())
