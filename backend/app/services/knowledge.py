"""Knowledge asset, ontology, multi-hop retrieval, and evidence services.

This module deliberately uses deterministic extraction and graph traversal.
It does not pretend that a lexical extractor is a trained domain model.  The
output is marked as ``proposed`` and cannot be published until a human reviewer
approves or rejects it.  An external model can replace the extraction methods
later without changing the API or evidence contracts.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException, ErrorCode
from app.models.knowledge import (
    DecisionEvidence,
    DecisionTrace,
    DecisionTraceStatus,
    KnowledgeAsset,
    KnowledgeAssetStatus,
    KnowledgeAssetType,
    KnowledgeAssetVersion,
    KnowledgeVersionStatus,
    OntologyNode,
    OntologyRelation,
    OntologyReviewStatus,
    OntologyVersion,
    OntologyVersionStatus,
)
from app.schemas.knowledge import (
    DecisionCreate,
    DecisionEvidenceIn,
    KnowledgeAssetCreate,
    KnowledgeAssetVersionCreate,
    KnowledgeSearchRequest,
    OntologyExtractRequest,
    OntologyVersionCreate,
)


ASSET_NOT_FOUND = 7001
ASSET_VERSION_NOT_FOUND = 7002
ONTOLOGY_VERSION_NOT_FOUND = 7003
ONTOLOGY_NODE_NOT_FOUND = 7004
ONTOLOGY_RELATION_NOT_FOUND = 7005
ONTOLOGY_REVIEW_INVALID = 7006
ONTOLOGY_NOT_PUBLISHABLE = 7007
DECISION_TRACE_NOT_FOUND = 7008
KNOWLEDGE_CONFLICT = 7009
KNOWLEDGE_INPUT_INVALID = 7010


_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{1,63}|[\u4e00-\u9fff]{2,32}")
_CHINESE_RE = re.compile(r"^[\u4e00-\u9fff]+$")
_SPLIT_RE = re.compile(r"[，。；：、,!?！？;:\s/\\|()（）\[\]【】<>《》“”\"'`]+")
_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "where",
    "policy", "data", "document", "system", "platform", "result",
    "关于", "以及", "进行", "相关", "问题", "情况", "工作", "管理", "一个",
    "我们", "他们", "可以", "需要", "通过", "根据", "对于", "其中", "进行",
    "数据", "文档", "系统", "平台", "结果", "信息", "内容", "方法",
}

_GENERIC_CANDIDATES = _STOPWORDS | {
    "意见", "征求", "征求意见", "征求意见稿", "通知", "公告", "公开", "标准",
    "环境", "海洋", "生态环境", "国家", "技术", "规范", "方案", "工作", "治理",
    "管理", "反馈", "联系人", "邮箱", "电话", "地址", "邮编", "附件", "单位",
    "名单", "编制说明", "要求", "单位名称", "公众", "网站", "发布", "实施",
    "查询", "登录", "办理", "承担", "负责", "申请", "审批", "许可", "设备",
    "垃圾",
    "监测", "处置", "执行", "回传", "照片", "数据", "编号", "时间", "结果",
    "事项", "工作", "相关", "有关", "进行", "情况", "问题", "内容", "方式",
}

# Seed heads are extraction hints, not gold labels.  They give the
# deterministic baseline a bounded place to expand a candidate phrase and
# avoid the all-n-gram explosion that produced fragments such as
# ``价规范`` and ``镇海漂垃圾``.
_CANDIDATE_HEADS = (
    "监督管理", "许可证", "摄像头", "机器人", "机械臂", "回收队伍",
    "在线监控", "政务服务", "行政许可", "海域", "监管部门",
    "红树林", "水质", "生态修复", "潮沟",
    "渔网", "网绳", "泡沫", "塑料", "垃圾", "废料", "疏浚物", "材料",
    "惰性无机地质材料",
    "台账", "回执", "闭环", "派单", "拾取", "研判", "复核", "缠绕",
    "称重", "风险", "规范", "导则", "指南", "法典", "许可证", "标准",
    "方案", "细则", "规则", "办法", "平台", "系统", "设备", "队伍",
    "镇", "县", "区", "乡", "村", "部", "厅", "司", "局", "物", "法",
)

_ADMIN_SUFFIXES = ("镇", "县", "区", "乡", "村")
_PROTECTED_HEADS = {
    "红树林", "水质", "潮沟", "海漂垃圾", "打捞机器人", "机械臂",
    "泡沫", "塑料", "渔网", "派单", "拾取",
    "惰性无机地质材料",
}
_ACTION_HEADS = (
    "值班", "研判", "复核", "派单", "拾取", "处置", "闭环",
    "称重", "回执", "回收", "队伍", "调度", "执行",
)
_DEVICE_HEADS = (
    "摄像头", "机器人", "机械臂", "在线监控", "设备", "平台",
)
_STANDARD_HEADS = (
    "规范", "导则", "指南", "标准", "法典", "法律", "办法", "许可证",
)
_PHRASE_BOUNDARIES = set(
    "，。；：、！？,;:()（）[]【】<>《》“”\"'` \n\t"
)
_FUNCTION_CHARS = set(
    "的了和与及或在对为是由通过按照根据包括主要应需其该本等并再从将向可实行以及"
    "我部各每这那有被把于以也仍均须即已未不无更最"
)
_TRAILING_ANNOTATIONS_RE = re.compile(
    r"[（(](?:征求意见稿|草案|演示|说明|修订稿|试行)[）)]"
)
_SENTENCE_RE = re.compile(r"[^。！？；!?;\n]+")

_RELATION_TYPE_NAMES = {
    "applies_to": "适用于",
    "includes": "包括或组成",
    "dispatches_to": "派单或调度至",
    "executes": "执行或处置",
    "monitors": "监测或研判",
    "uses": "使用或传输",
    "associated_with": "语义关联",
    "co_occurs_with": "在同一知识资产片段中共现",
}

_RELATION_CUE_RULES = (
    (re.compile(r"适用于|适用"), "applies_to", 7.0),
    (re.compile(r"派单|调度|下发|指令"), "dispatches_to", 6.0),
    (re.compile(r"包括|组成"), "includes", 5.0),
    (re.compile(r"执行|处置|拾取|回传|形成"), "executes", 4.0),
    (re.compile(r"监测|发现|监控|研判|复核"), "monitors", 4.0),
    (re.compile(r"使用|传输|依托"), "uses", 3.0),
)


@dataclass
class DocumentCandidate:
    asset_id: str
    asset_version_id: str
    title: str
    text: str
    asset_type: str
    source_uri: str | None
    standard_codes: list[str] = field(default_factory=list)


@dataclass
class TermCandidate:
    term: str
    canonical_name: str
    score: float
    source_asset_id: str
    source_version_id: str


@dataclass
class SearchHitData:
    asset_id: str
    asset_version_id: str
    title: str
    asset_type: str
    source_uri: str | None
    score: float
    snippet: str
    matched_node_ids: list[str] = field(default_factory=list)
    matched_relation_ids: list[str] = field(default_factory=list)
    hop_count: int = 0
    path: list[dict[str, Any]] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)


def new_business_id(prefix: str) -> str:
    """Return a compact, sortable-enough business identifier."""
    return f"{prefix}_{datetime.now(timezone.utc):%Y%m%d%H%M%S}_{uuid.uuid4().hex[:8]}"


def content_digest(
    content_text: str | None,
    content_json: dict[str, Any] | list[Any] | None,
) -> str:
    """Hash a version snapshot deterministically."""
    payload = json.dumps(
        {"text": content_text or "", "json": content_json},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dedupe_nonempty(values: Iterable[str], *, limit: int = 128) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for raw in values:
        value = str(raw).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
        if len(output) >= limit:
            break
    return output


def query_terms(text: str, *, limit: int = 24) -> list[str]:
    """Extract English terms and Chinese n-grams for deterministic retrieval."""
    terms: list[str] = []
    for chunk in _SPLIT_RE.split(text.lower()):
        chunk = chunk.strip()
        if not chunk:
            continue
        if _CHINESE_RE.fullmatch(chunk):
            if len(chunk) >= 2:
                terms.append(chunk)
            for size in (2, 3, 4):
                if len(chunk) <= size:
                    continue
                terms.extend(chunk[i : i + size] for i in range(len(chunk) - size + 1))
        else:
            terms.extend(_TERM_RE.findall(chunk))

    output: list[str] = []
    seen: set[str] = set()
    for term in terms:
        term = _normalize_term(term)
        if len(term) < 2 or term in _STOPWORDS or term in seen:
            continue
        seen.add(term)
        output.append(term)
        if len(output) >= limit:
            break
    return output


def _normalize_term(term: str) -> str:
    return term.strip().strip("._-").lower()


def score_document(query: str, document: DocumentCandidate) -> tuple[float, str]:
    """Score title, tags, content, and metadata without an opaque model."""
    terms = query_terms(query)
    if not terms:
        return 0.0, _snippet(document.text, [query]) if query else ""

    title = document.title.lower()
    text = document.text.lower()
    standards = " ".join(document.standard_codes).lower()
    title_hits = sum(1 for term in terms if term in title)
    text_hits = sum(text.count(term) for term in terms)
    standard_hits = sum(1 for term in terms if term in standards)
    score = title_hits * 6.0 + text_hits * 1.5 + standard_hits * 2.0
    return score, _snippet(f"{document.title} {document.text}", terms)


def _snippet(text: str, terms: list[str], *, limit: int = 320) -> str:
    clean = " ".join((text or "").split())
    if not clean:
        return ""
    lowered = clean.lower()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max(0, min(positions) - 60) if positions else 0
    excerpt = clean[start : start + limit]
    return ("..." if start else "") + excerpt + ("..." if start + limit < len(clean) else "")


def extract_term_candidates(
    documents: list[DocumentCandidate],
    *,
    max_nodes: int,
    min_term_length: int,
) -> list[TermCandidate]:
    """Extract frequency/title-weighted candidate phrases.

    The output is intentionally conservative.  Phrases that appear once still
    qualify when they occur in a title; all others need at least two mentions.
    """
    scores: defaultdict[str, float] = defaultdict(float)
    document_frequency: Counter[str] = Counter()
    title_frequency: Counter[str] = Counter()
    source: dict[str, tuple[str, str]] = {}

    for doc in documents:
        title_counts = _candidate_counts(doc.title, min_term_length)
        body_counts = _candidate_counts(doc.text, min_term_length)
        for term in set(title_counts) | set(body_counts):
            document_frequency[term] += 1
            source.setdefault(term, (doc.asset_id, doc.asset_version_id))
        for term, count in title_counts.items():
            title_frequency[term] += count
            scores[term] += count * 8.0
        for term, count in body_counts.items():
            scores[term] += count * 1.5

    candidates: list[TermCandidate] = []
    for key in set(scores) | set(document_frequency):
        title_count = title_frequency[key]
        frequency = document_frequency[key]
        if frequency < 2 and title_count == 0:
            continue
        score = scores[key] + frequency * 3.0 + len(key) * 0.1
        if any(key.endswith(suffix) for suffix in _ADMIN_SUFFIXES):
            score += 2.0
        if _is_standard_term(key):
            score += 5.0
        if any(head in key for head in _ACTION_HEADS):
            score += 12.0
        if any(head in key for head in _DEVICE_HEADS):
            score += 10.0
        if key[:1] in _ADMIN_SUFFIXES:
            score -= 24.0
        candidates.append(
            TermCandidate(
                term=key,
                canonical_name=key,
                score=score,
                source_asset_id=source[key][0],
                source_version_id=source[key][1],
            )
        )
    candidates.sort(key=lambda item: (-item.score, -len(item.term), item.term))

    # Equal-or-higher-score overlap pruning: when a longer phrase is already
    # kept, drop its contained sub-ngrams. This keeps the top-N proposal list
    # readable and reduces redundant human review without changing scores.
    kept: list[TermCandidate] = []
    for candidate in candidates:
        if candidate.canonical_name in _PROTECTED_HEADS:
            kept.append(candidate)
            continue
        if any(
            candidate.canonical_name != item.canonical_name
            and candidate.canonical_name in item.canonical_name
            and item.score >= candidate.score
            for item in kept
        ):
            continue
        kept.append(candidate)
    return kept[:max_nodes]


def _candidate_counts(text: str, min_term_length: int) -> Counter[str]:
    """Count conservative title/body candidates with short Chinese n-grams."""
    counts: Counter[str] = Counter()
    lowered = (text or "").lower()

    # Standards are commonly written as book-title phrases.  Preserve the
    # complete standard name and its meaningful title components before the
    # generic punctuation splitter can turn them into fragments.
    for raw_title in re.findall(r"《([^》]{2,120})》", lowered):
        cleaned_title = _TRAILING_ANNOTATIONS_RE.sub("", raw_title)
        for part in re.split(r"[\s，。；：、,!?！？;:/\\|()（）\[\]【】<>《》“”\"'`]+", cleaned_title):
            _add_standard_variants(counts, part, min_term_length)

    # Use bounded neighbourhoods around domain heads.  This is deliberately
    # stricter than all-n-gram expansion: a candidate must be anchored by a
    # known domain morpheme and may extend by only a few characters.
    for head in _CANDIDATE_HEADS:
        start = 0
        while True:
            position = lowered.find(head, start)
            if position < 0:
                break
            for left_size in range(0, 5):
                for right_size in range(0, 3):
                    phrase = lowered[
                        max(0, position - left_size) : position + len(head) + right_size
                    ]
                    normalized = _clean_candidate(phrase, min_term_length)
                    if normalized is not None:
                        counts[normalized] += 1
            start = position + len(head)

    for term in _TERM_RE.findall(lowered):
        normalized = _normalize_term(term)
        if normalized and not _CHINESE_RE.fullmatch(normalized):
            cleaned = _clean_candidate(normalized, min_term_length)
            if cleaned is not None:
                counts[cleaned] += 1
    return counts


def _add_standard_variants(
    counts: Counter[str],
    raw_part: str,
    min_term_length: int,
) -> None:
    part = _TRAILING_ANNOTATIONS_RE.sub("", raw_part).strip()
    cleaned = _clean_candidate(part, min_term_length)
    if cleaned is not None:
        counts[cleaned] += 1
    if not _CHINESE_RE.fullmatch(part):
        return
    for head in _STANDARD_HEADS:
        position = part.rfind(head)
        if position < 0:
            continue
        base = part[:position]
        for variant in (part, base, base[-12:]):
            cleaned_variant = _clean_candidate(variant, min_term_length)
            if cleaned_variant is not None:
                counts[cleaned_variant] += 1


def _clean_candidate(term: str, min_term_length: int) -> str | None:
    candidate = _TRAILING_ANNOTATIONS_RE.sub("", str(term or "")).strip()
    candidate = candidate.strip(" \t\r\n._-，。；：、,!?！？;:")
    if not candidate or any(char in _PHRASE_BOUNDARIES for char in candidate):
        return None
    if not re.fullmatch(r"[\w\u4e00-\u9fff.]+", candidate):
        return None
    while candidate and candidate[0] in _FUNCTION_CHARS:
        candidate = candidate[1:]
    while candidate and candidate[-1] in _FUNCTION_CHARS:
        candidate = candidate[:-1]
    if candidate in _GENERIC_CANDIDATES:
        return None
    if len(candidate) < min_term_length or len(candidate) > 16:
        return None
    if (
        any(char in _FUNCTION_CHARS for char in candidate)
        and candidate not in _PROTECTED_HEADS
    ):
        return None
    if candidate.isdigit():
        return None
    return _normalize_term(candidate)


def _is_standard_term(term: str) -> bool:
    return len(term) >= 6 and any(term.endswith(head) for head in _STANDARD_HEADS)


def cooccurrence_relations(
    documents: list[DocumentCandidate],
    nodes: list[TermCandidate],
    *,
    max_relations: int,
) -> list[dict[str, Any]]:
    """Build candidate relations from co-occurrence in source documents."""
    """Build scored candidate relations from sentence-local evidence.

    Shortest-distance same-sentence pairs receive the strongest score.  A
    small deterministic cue lexicon assigns a relation type when the text
    between two candidates contains an explicit predicate; otherwise the
    relation is kept as an untyped co-occurrence candidate for human review.
    """
    by_key = {node.canonical_name: node for node in nodes}
    pair_scores: defaultdict[tuple[str, str], float] = defaultdict(float)
    pair_counts: Counter[tuple[str, str]] = Counter()
    pair_distances: defaultdict[tuple[str, str], list[int]] = defaultdict(list)
    pair_document_ids: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
    pair_type_scores: defaultdict[tuple[str, str], defaultdict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    evidence: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    for doc in documents:
        segments = [(doc.title, True)] + [
            (match.group(0), False) for match in _SENTENCE_RE.finditer(doc.text)
        ]
        for segment, is_title in segments:
            mentions = _sentence_mentions(segment, nodes)
            for index, source_mention in enumerate(mentions):
                source_key = source_mention[2]
                for target_mention in mentions[index + 1 :]:
                    target_key = target_mention[2]
                    if source_key == target_key:
                        continue
                    edge = (source_key, target_key)
                    distance = max(0, target_mention[0] - source_mention[1])
                    between = segment[source_mention[1] : target_mention[0]]
                    relation_type, cue_score = _relation_type_for(
                        source_key,
                        target_key,
                        between,
                        is_title=is_title,
                    )
                    observation_score = _relation_observation_score(
                        distance=distance,
                        cue_score=cue_score,
                        is_title=is_title,
                    )
                    pair_scores[edge] += observation_score
                    pair_counts[edge] += 1
                    pair_distances[edge].append(distance)
                    pair_document_ids[edge].add(doc.asset_id)
                    pair_type_scores[edge][relation_type] += observation_score
                    if len(evidence[edge]) < 3:
                        evidence[edge].append(
                            {
                                "asset_id": doc.asset_id,
                                "asset_version_id": doc.asset_version_id,
                                "citation": _snippet(segment, [source_key, target_key]),
                            }
                        )

    ranked = sorted(
        pair_scores.items(),
        key=lambda item: (
            -item[1],
            -len(pair_document_ids[item[0]]),
            item[0][0],
            item[0][1],
        ),
    )
    output: list[dict[str, Any]] = []
    for edge, score in ranked[:max_relations]:
        source_key, target_key = edge
        type_scores = pair_type_scores[edge]
        relation_type = min(type_scores, key=lambda key: (-type_scores[key], key))
        output.append(
            {
                "source": by_key[source_key],
                "target": by_key[target_key],
                "count": pair_counts[edge],
                "relation_type": relation_type,
                "score": round(score, 4),
                "distance": min(pair_distances[edge]),
                "evidence": evidence[edge],
            }
        )
    return output


def _sentence_mentions(
    sentence: str,
    nodes: list[TermCandidate],
) -> list[tuple[int, int, str]]:
    lowered = sentence.lower()
    occurrences: list[tuple[int, int, str]] = []
    for node in nodes:
        key = node.canonical_name
        positions: list[int] = []
        start = 0
        while True:
            position = lowered.find(key, start)
            if position < 0:
                break
            positions.append(position)
            start = position + len(key)
        for position in positions:
            occurrences.append((position, position + len(key), key))

    selected: list[tuple[int, int, str]] = []
    for occurrence in sorted(
        occurrences,
        key=lambda item: (item[0], -(item[1] - item[0])),
    ):
        if any(
            occurrence[1] > existing[0] and occurrence[0] < existing[1]
            for existing in selected
        ):
            continue
        selected.append(occurrence)
    return selected


def _relation_type_for(
    source_key: str,
    target_key: str,
    between: str,
    *,
    is_title: bool,
) -> tuple[str, float]:
    if is_title and _is_standard_term(source_key) and not _is_standard_term(target_key):
        return "applies_to", 9.0
    for pattern, relation_type, cue_score in _RELATION_CUE_RULES:
        if pattern.search(between):
            return relation_type, cue_score
    return "co_occurs_with", 0.0


def _relation_observation_score(
    *,
    distance: int,
    cue_score: float,
    is_title: bool,
) -> float:
    if distance <= 12:
        distance_score = 5.0
    elif distance <= 32:
        distance_score = 3.0
    elif distance <= 80:
        distance_score = 1.0
    else:
        distance_score = 0.25
    return 2.0 + distance_score + cue_score + (2.0 if is_title else 0.0)


def relation_paths(
    seed_node_ids: set[str],
    relations: list[OntologyRelation],
    *,
    hop_depth: int,
) -> dict[str, list[dict[str, Any]]]:
    """Breadth-first traversal over approved ontology edges."""
    adjacency: defaultdict[str, list[OntologyRelation]] = defaultdict(list)
    for relation in relations:
        if relation.review_status != OntologyReviewStatus.APPROVED:
            continue
        adjacency[relation.source_node_id].append(relation)
        adjacency[relation.target_node_id].append(relation)

    paths: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in seed_node_ids}
    queue: deque[tuple[str, int]] = deque((node_id, 0) for node_id in seed_node_ids)
    while queue:
        node_id, depth = queue.popleft()
        if depth >= hop_depth:
            continue
        for relation in adjacency.get(node_id, []):
            if relation.source_node_id == node_id:
                target = relation.target_node_id
            elif relation.target_node_id == node_id:
                target = relation.source_node_id
            else:
                continue
            if target in paths:
                continue
            paths[target] = paths[node_id] + [
                {
                    "hop_no": depth + 1,
                    "source_node_id": node_id,
                    "relation_id": relation.relation_id,
                    "target_node_id": target,
                    "relation_type": relation.relation_type,
                }
            ]
            queue.append((target, depth + 1))
    return paths


class KnowledgeService:
    """Database-backed knowledge operations shared by HTTP and Nexent MCP."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_asset(
        self,
        payload: KnowledgeAssetCreate,
        *,
        username: str,
        township_scope: str | None = None,
    ) -> tuple[KnowledgeAsset, KnowledgeAssetVersion]:
        township = payload.township
        if township_scope:
            if township and township != township_scope:
                raise AppException(
                    code=ErrorCode.FORBIDDEN,
                    message="当前账号无权在其他辖区登记知识资产",
                    http_status=403,
                )
            township = township_scope

        asset_id = payload.asset_id or new_business_id("ast")
        existing = await self.session.scalar(
            select(KnowledgeAsset).where(KnowledgeAsset.asset_id == asset_id)
        )
        if existing is not None:
            raise AppException(
                code=KNOWLEDGE_CONFLICT,
                message=f"知识资产 {asset_id} 已存在",
                http_status=200,
            )

        asset = KnowledgeAsset(
            asset_id=asset_id,
            asset_type=payload.asset_type,
            title=payload.title,
            description=payload.description,
            source_uri=payload.source_uri,
            source_system=payload.source_system,
            mime_type=payload.mime_type,
            region=payload.region,
            township=township,
            security_level=payload.security_level,
            status=payload.status,
            current_version=1,
            standard_codes=_dedupe_nonempty(payload.standard_codes),
            tags=_dedupe_nonempty(payload.tags),
            attributes_json=dict(payload.attributes),
            created_by=username,
            updated_by=username,
        )
        self.session.add(asset)

        initial = payload.initial_content or KnowledgeAssetVersionCreate()
        version = self._build_asset_version(asset_id, 1, initial, username)
        self.session.add(version)
        await self.session.flush()
        return asset, version

    def _build_asset_version(
        self,
        asset_id: str,
        version_no: int,
        payload: KnowledgeAssetVersionCreate,
        username: str,
    ) -> KnowledgeAssetVersion:
        return KnowledgeAssetVersion(
            version_id=new_business_id("kav"),
            asset_id=asset_id,
            version_no=version_no,
            status=KnowledgeVersionStatus.ACTIVE,
            content_hash=content_digest(payload.content_text, payload.content_json),
            content_text=payload.content_text,
            content_json=payload.content_json,
            extraction_method=payload.extraction_method,
            extraction_confidence=(
                Decimal(str(payload.extraction_confidence))
                if payload.extraction_confidence is not None
                else None
            ),
            language=payload.language,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
            metadata_json=dict(payload.metadata),
            created_by=username,
        )

    async def append_asset_version(
        self,
        asset_id: str,
        payload: KnowledgeAssetVersionCreate,
        *,
        username: str,
    ) -> KnowledgeAssetVersion:
        asset = await self.session.scalar(
            select(KnowledgeAsset).where(KnowledgeAsset.asset_id == asset_id)
        )
        if asset is None:
            raise AppException(
                code=ASSET_NOT_FOUND,
                message=f"知识资产 {asset_id} 不存在",
                http_status=200,
            )
        previous = (
            await self.session.scalars(
                select(KnowledgeAssetVersion).where(
                    KnowledgeAssetVersion.asset_id == asset_id,
                    KnowledgeAssetVersion.status == KnowledgeVersionStatus.ACTIVE,
                )
            )
        ).all()
        for version in previous:
            version.status = KnowledgeVersionStatus.SUPERSEDED

        next_no = asset.current_version + 1
        asset.current_version = next_no
        asset.updated_by = username
        version = self._build_asset_version(asset_id, next_no, payload, username)
        self.session.add(version)
        await self.session.flush()
        return version

    async def list_assets(
        self,
        *,
        asset_type: str | None,
        status: str | None,
        region: str | None,
        township: str | None,
        query: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[KnowledgeAsset], int]:
        conditions = []
        if asset_type:
            conditions.append(KnowledgeAsset.asset_type == asset_type)
        if status:
            conditions.append(KnowledgeAsset.status == status)
        if region:
            conditions.append(KnowledgeAsset.region == region)
        if township:
            conditions.append(KnowledgeAsset.township == township)
        if query:
            pattern = f"%{query}%"
            conditions.append(
                or_(
                    KnowledgeAsset.title.ilike(pattern),
                    KnowledgeAsset.description.ilike(pattern),
                    KnowledgeAsset.asset_id.ilike(pattern),
                )
            )

        total_stmt = select(func.count(KnowledgeAsset.id))
        stmt = select(KnowledgeAsset)
        if conditions:
            total_stmt = total_stmt.where(*conditions)
            stmt = stmt.where(*conditions)
        total = int((await self.session.scalar(total_stmt)) or 0)
        rows = (
            await self.session.scalars(
                stmt.order_by(KnowledgeAsset.updated_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return list(rows), total

    async def get_asset(
        self,
        asset_id: str,
    ) -> tuple[KnowledgeAsset, list[KnowledgeAssetVersion]]:
        asset = await self.session.scalar(
            select(KnowledgeAsset).where(KnowledgeAsset.asset_id == asset_id)
        )
        if asset is None:
            raise AppException(
                code=ASSET_NOT_FOUND,
                message=f"知识资产 {asset_id} 不存在",
                http_status=200,
            )
        versions = (
            await self.session.scalars(
                select(KnowledgeAssetVersion)
                .where(KnowledgeAssetVersion.asset_id == asset_id)
                .order_by(KnowledgeAssetVersion.version_no.desc())
            )
        ).all()
        return asset, list(versions)

    async def get_asset_version(self, version_id: str) -> KnowledgeAssetVersion:
        version = await self.session.scalar(
            select(KnowledgeAssetVersion).where(
                KnowledgeAssetVersion.version_id == version_id
            )
        )
        if version is None:
            raise AppException(
                code=ASSET_VERSION_NOT_FOUND,
                message=f"知识资产版本 {version_id} 不存在",
                http_status=200,
            )
        return version

    async def create_ontology_version(
        self,
        payload: OntologyVersionCreate,
        *,
        username: str,
    ) -> OntologyVersion:
        version_no = payload.version_no
        if version_no is None:
            max_no = await self.session.scalar(
                select(func.max(OntologyVersion.version_no)).where(
                    OntologyVersion.name == payload.name
                )
            )
            version_no = int(max_no or 0) + 1

        existing = await self.session.scalar(
            select(OntologyVersion).where(
                OntologyVersion.name == payload.name,
                OntologyVersion.version_no == version_no,
            )
        )
        if existing is not None:
            raise AppException(
                code=KNOWLEDGE_CONFLICT,
                message=f"本体 {payload.name} v{version_no} 已存在",
                http_status=200,
            )
        if payload.parent_version_id:
            parent = await self.session.scalar(
                select(OntologyVersion).where(
                    OntologyVersion.version_id == payload.parent_version_id
                )
            )
            if parent is None:
                raise AppException(
                    code=ONTOLOGY_VERSION_NOT_FOUND,
                    message=f"父本体版本 {payload.parent_version_id} 不存在",
                    http_status=200,
                )

        version = OntologyVersion(
            version_id=new_business_id("ont"),
            name=payload.name,
            version_no=version_no,
            status=OntologyVersionStatus.DRAFT,
            parent_version_id=payload.parent_version_id,
            description=payload.description,
            standard_codes=_dedupe_nonempty(payload.standard_codes),
            metadata_json=dict(payload.metadata),
            created_by=username,
        )
        self.session.add(version)
        await self.session.flush()
        return version

    async def list_ontology_versions(self) -> list[OntologyVersion]:
        """Return every ontology version, newest package first."""
        rows = (
            await self.session.scalars(
                select(OntologyVersion).order_by(
                    OntologyVersion.created_at.desc(),
                    OntologyVersion.name,
                    OntologyVersion.version_no.desc(),
                )
            )
        ).all()
        return list(rows)

    async def get_ontology_version(self, version_id: str) -> OntologyVersion:
        version = await self.session.scalar(
            select(OntologyVersion).where(OntologyVersion.version_id == version_id)
        )
        if version is None:
            raise AppException(
                code=ONTOLOGY_VERSION_NOT_FOUND,
                message=f"本体版本 {version_id} 不存在",
                http_status=200,
            )
        return version

    async def list_ontology_nodes(self, version_id: str) -> list[OntologyNode]:
        await self.get_ontology_version(version_id)
        rows = (
            await self.session.scalars(
                select(OntologyNode)
                .where(OntologyNode.ontology_version_id == version_id)
                .order_by(OntologyNode.confidence.desc(), OntologyNode.name)
            )
        ).all()
        return list(rows)

    async def list_ontology_relations(self, version_id: str) -> list[OntologyRelation]:
        await self.get_ontology_version(version_id)
        rows = (
            await self.session.scalars(
                select(OntologyRelation)
                .where(OntologyRelation.ontology_version_id == version_id)
                .order_by(OntologyRelation.confidence.desc(), OntologyRelation.relation_id)
            )
        ).all()
        return list(rows)

    async def extract_ontology(
        self,
        payload: OntologyExtractRequest,
        *,
        username: str,
        township_scope: str | None = None,
    ) -> tuple[OntologyVersion, list[OntologyNode], list[OntologyRelation], int, int]:
        version = await self.get_ontology_version(payload.ontology_version_id)
        if version.status not in (OntologyVersionStatus.DRAFT, OntologyVersionStatus.IN_REVIEW):
            raise AppException(
                code=ONTOLOGY_REVIEW_INVALID,
                message=f"本体版本状态 {version.status} 不允许继续抽取候选",
                http_status=200,
            )

        rows = (
            await self.session.execute(
                select(KnowledgeAssetVersion, KnowledgeAsset)
                .join(
                    KnowledgeAsset,
                    KnowledgeAsset.asset_id == KnowledgeAssetVersion.asset_id,
                )
                .where(KnowledgeAssetVersion.version_id.in_(payload.asset_version_ids))
            )
        ).all()
        if len(rows) != len(set(payload.asset_version_ids)):
            found = {version_row.version_id for version_row, _asset in rows}
            missing = sorted(set(payload.asset_version_ids) - found)
            raise AppException(
                code=ASSET_VERSION_NOT_FOUND,
                message=f"知识资产版本不存在: {', '.join(missing)}",
                http_status=200,
            )
        if township_scope:
            out_of_scope = sorted(
                {
                    asset.asset_id
                    for _asset_version, asset in rows
                    if asset.township != township_scope
                }
            )
            if out_of_scope:
                raise AppException(
                    code=ErrorCode.FORBIDDEN,
                    message="当前账号无权抽取其他辖区知识资产",
                    http_status=403,
                    detail={"asset_ids": out_of_scope},
                )

        documents = [
            DocumentCandidate(
                asset_id=asset.asset_id,
                asset_version_id=asset_version.version_id,
                title=asset.title,
                text=asset_version.content_text
                or json.dumps(asset_version.content_json or {}, ensure_ascii=False),
                asset_type=asset.asset_type,
                source_uri=asset.source_uri,
                standard_codes=list(asset.standard_codes or []),
            )
            for asset_version, asset in rows
        ]
        candidates = extract_term_candidates(
            documents,
            max_nodes=payload.max_nodes,
            min_term_length=payload.min_term_length,
        )

        existing_nodes = (
            await self.session.scalars(
                select(OntologyNode).where(
                    OntologyNode.ontology_version_id == version.version_id
                )
            )
        ).all()
        node_by_key = {node.canonical_name: node for node in existing_nodes}
        created_nodes: list[OntologyNode] = []
        for candidate in candidates:
            if candidate.canonical_name in node_by_key:
                continue
            confidence = min(0.95, 0.45 + candidate.score * 0.05)
            node = OntologyNode(
                node_id=new_business_id("nod"),
                ontology_version_id=version.version_id,
                entity_type=_entity_type_for_asset(
                    next(
                        doc.asset_type
                        for doc in documents
                        if doc.asset_version_id == candidate.source_version_id
                    )
                ),
                name=candidate.term,
                canonical_name=candidate.canonical_name,
                description=f"从资产 {candidate.source_asset_id} 半自动抽取的候选实体",
                aliases=[],
                properties_json={"extraction_score": candidate.score},
                source_asset_id=candidate.source_asset_id,
                source_version_id=candidate.source_version_id,
                confidence=Decimal(str(round(confidence, 4))),
                review_status=OntologyReviewStatus.PROPOSED,
            )
            self.session.add(node)
            node_by_key[candidate.canonical_name] = node
            created_nodes.append(node)

        candidate_by_key = {candidate.canonical_name: candidate for candidate in candidates}
        all_candidate_nodes = [
            candidate_by_key.get(node.canonical_name)
            or TermCandidate(
                term=node.name,
                canonical_name=node.canonical_name,
                score=float(node.confidence or 0),
                source_asset_id=node.source_asset_id or "",
                source_version_id=node.source_version_id or "",
            )
            for node in node_by_key.values()
        ]
        relation_specs = cooccurrence_relations(
            documents,
            all_candidate_nodes,
            max_relations=payload.max_relations,
        )

        existing_relations = (
            await self.session.scalars(
                select(OntologyRelation).where(
                    OntologyRelation.ontology_version_id == version.version_id
                )
            )
        ).all()
        relation_keys = {
            (
                relation.source_node_id,
                relation.target_node_id,
                relation.relation_type,
            )
            for relation in existing_relations
        }
        created_relations: list[OntologyRelation] = []
        for spec in relation_specs:
            source = node_by_key[spec["source"].canonical_name]
            target = node_by_key[spec["target"].canonical_name]
            relation_type = spec["relation_type"]
            key = (source.node_id, target.node_id, relation_type)
            reverse_key = (target.node_id, source.node_id, relation_type)
            if key in relation_keys or reverse_key in relation_keys:
                continue
            confidence = min(0.9, 0.45 + spec["score"] * 0.015)
            relation = OntologyRelation(
                relation_id=new_business_id("rel"),
                ontology_version_id=version.version_id,
                source_node_id=source.node_id,
                target_node_id=target.node_id,
                relation_type=relation_type,
                description=_RELATION_TYPE_NAMES.get(relation_type, relation_type),
                properties_json={
                    "cooccurrence_count": spec["count"],
                    "extraction_score": spec["score"],
                    "min_distance": spec["distance"],
                },
                evidence_json=spec["evidence"],
                confidence=Decimal(str(round(confidence, 4))),
                review_status=OntologyReviewStatus.PROPOSED,
            )
            self.session.add(relation)
            relation_keys.add(key)
            created_relations.append(relation)

        version.status = OntologyVersionStatus.IN_REVIEW
        version.metadata_json = {
            **dict(version.metadata_json or {}),
            "last_extracted_by": username,
            "last_extracted_at": datetime.now(timezone.utc).isoformat(),
        }
        await self.session.flush()
        # ``updated_at`` is populated by the database and may be expired after
        # flush.  Refresh before the API serializer reads it.
        await self.session.refresh(version)
        return version, created_nodes, created_relations, len(created_nodes), len(created_relations)

    async def review_node(
        self,
        node_id: str,
        decision: str,
        *,
        username: str,
        reason: str | None,
    ) -> OntologyNode:
        node = await self.session.scalar(
            select(OntologyNode).where(OntologyNode.node_id == node_id)
        )
        if node is None:
            raise AppException(
                code=ONTOLOGY_NODE_NOT_FOUND,
                message=f"本体节点 {node_id} 不存在",
                http_status=200,
            )
        await self._ensure_reviewable(node.ontology_version_id)
        node.review_status = decision
        node.reviewed_by = username
        node.reviewed_at = datetime.now(timezone.utc)
        node.properties_json = {
            **dict(node.properties_json or {}),
            "review_reason": reason,
        }
        await self.session.flush()
        await self.session.refresh(node)
        return node

    async def review_relation(
        self,
        relation_id: str,
        decision: str,
        *,
        username: str,
        reason: str | None,
    ) -> OntologyRelation:
        relation = await self.session.scalar(
            select(OntologyRelation).where(OntologyRelation.relation_id == relation_id)
        )
        if relation is None:
            raise AppException(
                code=ONTOLOGY_RELATION_NOT_FOUND,
                message=f"本体关系 {relation_id} 不存在",
                http_status=200,
            )
        await self._ensure_reviewable(relation.ontology_version_id)
        relation.review_status = decision
        relation.reviewed_by = username
        relation.reviewed_at = datetime.now(timezone.utc)
        relation.properties_json = {
            **dict(relation.properties_json or {}),
            "review_reason": reason,
        }
        await self.session.flush()
        await self.session.refresh(relation)
        return relation

    async def _ensure_reviewable(self, version_id: str) -> OntologyVersion:
        version = await self.get_ontology_version(version_id)
        if version.status not in (OntologyVersionStatus.DRAFT, OntologyVersionStatus.IN_REVIEW):
            raise AppException(
                code=ONTOLOGY_REVIEW_INVALID,
                message=f"本体版本状态 {version.status} 不允许审核",
                http_status=200,
            )
        return version

    async def publish_ontology(
        self,
        version_id: str,
        *,
        username: str,
    ) -> tuple[OntologyVersion, dict[str, int]]:
        version = await self.get_ontology_version(version_id)
        if version.status not in (OntologyVersionStatus.DRAFT, OntologyVersionStatus.IN_REVIEW):
            raise AppException(
                code=ONTOLOGY_REVIEW_INVALID,
                message=f"本体版本状态 {version.status} 不允许发布",
                http_status=200,
            )
        nodes = await self.list_ontology_nodes(version_id)
        relations = await self.list_ontology_relations(version_id)
        if any(node.review_status == OntologyReviewStatus.PROPOSED for node in nodes):
            raise AppException(
                code=ONTOLOGY_NOT_PUBLISHABLE,
                message="仍有未审核节点，禁止发布本体",
                http_status=200,
            )
        if any(relation.review_status == OntologyReviewStatus.PROPOSED for relation in relations):
            raise AppException(
                code=ONTOLOGY_NOT_PUBLISHABLE,
                message="仍有未审核关系，禁止发布本体",
                http_status=200,
            )
        if not any(node.review_status == OntologyReviewStatus.APPROVED for node in nodes):
            raise AppException(
                code=ONTOLOGY_NOT_PUBLISHABLE,
                message="本体至少需要一个已审核通过的节点",
                http_status=200,
            )

        previous_versions = (
            await self.session.scalars(
                select(OntologyVersion).where(
                    OntologyVersion.name == version.name,
                    OntologyVersion.status == OntologyVersionStatus.PUBLISHED,
                    OntologyVersion.version_id != version.version_id,
                )
            )
        ).all()
        for previous in previous_versions:
            previous.status = OntologyVersionStatus.RETIRED

        version.status = OntologyVersionStatus.PUBLISHED
        version.reviewed_by = username
        version.reviewed_at = datetime.now(timezone.utc)
        version.published_by = username
        version.published_at = datetime.now(timezone.utc)
        await self.session.flush()
        await self.session.refresh(version)
        return version, {
            "approved_nodes": sum(
                node.review_status == OntologyReviewStatus.APPROVED for node in nodes
            ),
            "rejected_nodes": sum(
                node.review_status == OntologyReviewStatus.REJECTED for node in nodes
            ),
            "approved_relations": sum(
                relation.review_status == OntologyReviewStatus.APPROVED
                for relation in relations
            ),
            "rejected_relations": sum(
                relation.review_status == OntologyReviewStatus.REJECTED
                for relation in relations
            ),
        }

    async def search(
        self,
        payload: KnowledgeSearchRequest,
        *,
        township_scope: str | None = None,
    ) -> tuple[str, str | None, list[SearchHitData]]:
        ontology = None
        if payload.ontology_version_id:
            ontology = await self.get_ontology_version(payload.ontology_version_id)
            if ontology.status != OntologyVersionStatus.PUBLISHED:
                raise AppException(
                    code=ONTOLOGY_REVIEW_INVALID,
                    message="多跳检索只允许使用已发布本体",
                    http_status=200,
                )
        else:
            ontology = await self.session.scalar(
                select(OntologyVersion)
                .where(OntologyVersion.status == OntologyVersionStatus.PUBLISHED)
                .order_by(OntologyVersion.published_at.desc())
                .limit(1)
            )

        conditions = [
            KnowledgeAsset.status == KnowledgeAssetStatus.ACTIVE,
            KnowledgeAsset.current_version == KnowledgeAssetVersion.version_no,
        ]
        if payload.asset_types:
            conditions.append(KnowledgeAsset.asset_type.in_(payload.asset_types))
        if township_scope:
            conditions.append(KnowledgeAsset.township == township_scope)

        rows = (
            await self.session.execute(
                select(KnowledgeAssetVersion, KnowledgeAsset)
                .join(
                    KnowledgeAsset,
                    KnowledgeAsset.asset_id == KnowledgeAssetVersion.asset_id,
                )
                .where(*conditions)
                .order_by(KnowledgeAsset.updated_at.desc())
                .limit(1000)
            )
        ).all()
        documents = [
            DocumentCandidate(
                asset_id=asset.asset_id,
                asset_version_id=asset_version.version_id,
                title=asset.title,
                text=asset_version.content_text
                or json.dumps(asset_version.content_json or {}, ensure_ascii=False),
                asset_type=asset.asset_type,
                source_uri=asset.source_uri,
                standard_codes=list(asset.standard_codes or []),
            )
            for asset_version, asset in rows
        ]
        if payload.standard_codes:
            required = set(payload.standard_codes)
            documents = [
                doc for doc in documents if required.intersection(doc.standard_codes)
            ]

        nodes: list[OntologyNode] = []
        relations: list[OntologyRelation] = []
        if ontology is not None:
            nodes = await self.list_ontology_nodes(ontology.version_id)
            relations = await self.list_ontology_relations(ontology.version_id)
        node_by_id = {node.node_id: node for node in nodes}
        terms = query_terms(payload.query)
        seed_nodes = {
            node.node_id
            for node in nodes
            if node.review_status == OntologyReviewStatus.APPROVED
            and _matches_terms(
                " ".join([node.name, node.canonical_name, *(node.aliases or [])]),
                terms,
            )
        }
        paths = relation_paths(seed_nodes, relations, hop_depth=payload.hop_depth)
        node_depth = {node_id: len(path) for node_id, path in paths.items()}

        hits: list[SearchHitData] = []
        for document in documents:
            direct_score, snippet = score_document(payload.query, document)
            matched_nodes = [
                node
                for node in nodes
                if node.source_version_id == document.asset_version_id
                and node.review_status == OntologyReviewStatus.APPROVED
                and node.node_id in node_depth
            ]
            graph_score = sum(
                3.0 / (1 + node_depth[node.node_id]) for node in matched_nodes
            )
            score = direct_score + graph_score
            if score <= 0:
                continue
            path_steps: list[dict[str, Any]] = []
            relation_ids: set[str] = set()
            for node in matched_nodes:
                for step in paths.get(node.node_id, []):
                    relation_ids.add(step["relation_id"])
                    if step not in path_steps:
                        path_steps.append(step)
            hop_count = max(
                (len(paths.get(node.node_id, [])) for node in matched_nodes),
                default=0,
            )
            citations = [
                f"{document.title} [{document.asset_version_id}]"
            ]
            for step in path_steps:
                source_node = node_by_id.get(step["source_node_id"])
                target_node = node_by_id.get(step["target_node_id"])
                citations.append(
                    f"{source_node.name if source_node else step['source_node_id']} "
                    f"-[{step['relation_type']}]-> "
                    f"{target_node.name if target_node else step['target_node_id']}"
                )
            hits.append(
                SearchHitData(
                    asset_id=document.asset_id,
                    asset_version_id=document.asset_version_id,
                    title=document.title,
                    asset_type=document.asset_type,
                    source_uri=document.source_uri,
                    score=round(score, 6),
                    snippet=snippet,
                    matched_node_ids=sorted({node.node_id for node in matched_nodes}),
                    matched_relation_ids=sorted(relation_ids),
                    hop_count=hop_count,
                    path=path_steps,
                    citations=citations,
                )
            )

        hits.sort(key=lambda hit: (-hit.score, hit.title))
        return (
            "ontology_graph" if ontology is not None else "keyword",
            ontology.version_id if ontology is not None else None,
            hits[: payload.limit],
        )

    async def create_decision(
        self,
        payload: DecisionCreate,
        *,
        username: str,
        township_scope: str | None = None,
    ) -> DecisionTrace:
        evidence_payload = list(payload.evidence)
        search_meta: dict[str, Any] = {}
        if not evidence_payload:
            _mode, ontology_version_id, hits = await self.search(
                KnowledgeSearchRequest(
                    query=payload.question,
                    ontology_version_id=payload.ontology_version_id,
                    hop_depth=payload.hop_depth,
                    limit=10,
                ),
                township_scope=township_scope,
            )
            search_meta = {
                "search_mode": _mode,
                "ontology_version_id": ontology_version_id,
                "result_count": len(hits),
            }
            # A decision trace is a path-shaped artifact: keep direct matches
            # ahead of bridge documents so readers can follow the evidence
            # chain instead of seeing it reordered by a question-specific
            # lexical score.
            for hit in sorted(
                hits,
                key=lambda item: (item.hop_count, -item.score, item.title),
            ):
                evidence_payload.append(
                    DecisionEvidenceIn(
                        asset_id=hit.asset_id,
                        asset_version_id=hit.asset_version_id,
                        relation_id=(
                            hit.matched_relation_ids[0]
                            if hit.matched_relation_ids
                            else None
                        ),
                        hop_no=hit.hop_count,
                        citation_text=hit.citations[0]
                        if hit.citations
                        else hit.snippet or hit.title,
                        source_uri=hit.source_uri,
                        score=hit.score,
                        metadata={
                            "citations": hit.citations,
                            "path": hit.path,
                            "matched_node_ids": hit.matched_node_ids,
                        },
                    )
                )

        await self._ensure_evidence_scope(evidence_payload, township_scope)

        trace = DecisionTrace(
            trace_id=new_business_id("dtr"),
            run_id=payload.run_id,
            question=payload.question,
            answer_summary=payload.answer_summary,
            status=(
                DecisionTraceStatus.COMPLETED
                if evidence_payload
                else DecisionTraceStatus.INSUFFICIENT_EVIDENCE
            ),
            ontology_version_id=payload.ontology_version_id,
            policy_version=payload.policy_version,
            metadata_json={**dict(payload.metadata), **search_meta},
            created_by=username,
        )
        self.session.add(trace)
        # The evidence rows reference trace_id through a plain FK, not an ORM
        # relationship, so persist the trace first to guarantee insert order.
        await self.session.flush()
        for rank, item in enumerate(evidence_payload, start=1):
            self.session.add(
                DecisionEvidence(
                    evidence_id=new_business_id("evd"),
                    trace_id=trace.trace_id,
                    rank_no=rank,
                    asset_id=item.asset_id,
                    asset_version_id=item.asset_version_id,
                    node_id=item.node_id,
                    relation_id=item.relation_id,
                    hop_no=item.hop_no,
                    citation_text=item.citation_text,
                    source_uri=item.source_uri,
                    score=Decimal(str(round(item.score, 6))),
                    metadata_json=dict(item.metadata),
                )
            )
        await self.session.flush()
        return trace

    async def _ensure_evidence_scope(
        self,
        evidence: list[DecisionEvidenceIn],
        township_scope: str | None,
    ) -> None:
        """Reject direct evidence references outside an operator's township."""
        if not township_scope or not evidence:
            return

        version_ids = {
            item.asset_version_id
            for item in evidence
            if item.asset_version_id
        }
        asset_ids = {
            item.asset_id
            for item in evidence
            if item.asset_id
        }

        if version_ids:
            version_rows = (
                await self.session.execute(
                    select(KnowledgeAssetVersion, KnowledgeAsset)
                    .join(
                        KnowledgeAsset,
                        KnowledgeAsset.asset_id == KnowledgeAssetVersion.asset_id,
                    )
                    .where(KnowledgeAssetVersion.version_id.in_(version_ids))
                )
            ).all()
            found_versions = {
                asset_version.version_id for asset_version, _asset in version_rows
            }
            missing_versions = sorted(version_ids - found_versions)
            if missing_versions:
                raise AppException(
                    code=ASSET_VERSION_NOT_FOUND,
                    message=f"知识资产版本不存在: {', '.join(missing_versions)}",
                    http_status=200,
                )
            if any(asset.township != township_scope for _version, asset in version_rows):
                raise AppException(
                    code=ErrorCode.FORBIDDEN,
                    message="决策证据包含其他辖区知识资产",
                    http_status=403,
                )

        if asset_ids:
            assets = (
                await self.session.scalars(
                    select(KnowledgeAsset).where(KnowledgeAsset.asset_id.in_(asset_ids))
                )
            ).all()
            found_assets = {asset.asset_id for asset in assets}
            missing_assets = sorted(asset_ids - found_assets)
            if missing_assets:
                raise AppException(
                    code=ASSET_NOT_FOUND,
                    message=f"知识资产不存在: {', '.join(missing_assets)}",
                    http_status=200,
                )
            if any(asset.township != township_scope for asset in assets):
                raise AppException(
                    code=ErrorCode.FORBIDDEN,
                    message="决策证据包含其他辖区知识资产",
                    http_status=403,
                )

    async def get_decision(self, trace_id: str) -> tuple[DecisionTrace, list[DecisionEvidence]]:
        trace = await self.session.scalar(
            select(DecisionTrace).where(DecisionTrace.trace_id == trace_id)
        )
        if trace is None:
            raise AppException(
                code=DECISION_TRACE_NOT_FOUND,
                message=f"决策轨迹 {trace_id} 不存在",
                http_status=200,
            )
        evidence = (
            await self.session.scalars(
                select(DecisionEvidence)
                .where(DecisionEvidence.trace_id == trace_id)
                .order_by(DecisionEvidence.rank_no)
            )
        ).all()
        return trace, list(evidence)

    async def list_decisions(
        self,
        *,
        run_id: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[DecisionTrace], int]:
        conditions = []
        if run_id:
            conditions.append(DecisionTrace.run_id == run_id)
        total_stmt = select(func.count(DecisionTrace.id))
        stmt = select(DecisionTrace)
        if conditions:
            total_stmt = total_stmt.where(*conditions)
            stmt = stmt.where(*conditions)
        total = int((await self.session.scalar(total_stmt)) or 0)
        rows = (
            await self.session.scalars(
                stmt.order_by(DecisionTrace.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return list(rows), total


def _entity_type_for_asset(asset_type: str) -> str:
    return {
        KnowledgeAssetType.DOCUMENT: "concept",
        KnowledgeAssetType.TABLE: "metric",
        KnowledgeAssetType.IMAGE: "observation",
        KnowledgeAssetType.EVENT: "event",
        KnowledgeAssetType.TELEMETRY: "measurement",
        KnowledgeAssetType.DATASET: "dataset",
    }.get(asset_type, "concept")


def _matches_terms(value: str, terms: list[str]) -> bool:
    lowered = value.lower()
    return any(term in lowered for term in terms)
