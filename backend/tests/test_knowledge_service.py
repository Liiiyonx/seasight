"""Knowledge-domain logic and service regression tests.

The service tests use an in-memory SQLite database with only the seven
knowledge tables.  This keeps the core ontology/evidence loop testable without
PostgreSQL, Redis, MQTT, or robot infrastructure.
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.exceptions import AppException
from app.db.session import Base
from app.models.knowledge import (
    DecisionEvidence,
    DecisionTrace,
    KnowledgeAsset,
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
    KnowledgeAssetCreate,
    KnowledgeAssetVersionCreate,
    KnowledgeSearchRequest,
    OntologyExtractRequest,
    OntologyVersionCreate,
)
from app.services.knowledge import (
    DocumentCandidate,
    ONTOLOGY_NOT_PUBLISHABLE,
    TermCandidate,
    KnowledgeService,
    cooccurrence_relations,
    content_digest,
    extract_term_candidates,
    query_terms,
    relation_paths,
    score_document,
)


KNOWLEDGE_TABLES = [
    KnowledgeAsset.__table__,
    KnowledgeAssetVersion.__table__,
    OntologyVersion.__table__,
    OntologyNode.__table__,
    OntologyRelation.__table__,
    DecisionTrace.__table__,
    DecisionEvidence.__table__,
]


def test_content_digest_is_stable_across_json_key_order() -> None:
    first = content_digest("海面塑料垃圾", {"b": 2, "a": 1})
    second = content_digest("海面塑料垃圾", {"a": 1, "b": 2})
    changed = content_digest("海面塑料垃圾", {"a": 1, "b": 3})

    assert first == second
    assert first != changed
    assert len(first) == 64


def test_query_terms_extracts_chinese_ngrams_and_english_identifiers() -> None:
    terms = query_terms("红树林生态修复，使用 GB17378.4 和 data_quality 标准")

    assert "红树林" in terms
    assert "生态" in terms
    assert "修复" in terms
    assert "gb17378.4" in terms
    assert "data_quality" in terms
    assert len(terms) <= 24


def test_extract_terms_and_cooccurrence_keep_source_evidence() -> None:
    documents = [
        DocumentCandidate(
            asset_id="ast_1",
            asset_version_id="kav_1",
            title="红树林生态修复规范",
            text="红树林生态修复需要监测水质。红树林生态修复应保留潮沟。",
            asset_type=KnowledgeAssetType.DOCUMENT,
            source_uri="policy://red-mangrove",
            standard_codes=["HY/T 1234"],
        ),
        DocumentCandidate(
            asset_id="ast_2",
            asset_version_id="kav_2",
            title="海洋水质监测记录",
            text="红树林生态修复区域水质监测结果正常。",
            asset_type=KnowledgeAssetType.TABLE,
            source_uri="table://water-quality",
            standard_codes=[],
        ),
    ]

    nodes = extract_term_candidates(documents, max_nodes=20, min_term_length=2)
    assert any("红树林" in node.canonical_name for node in nodes)
    assert all(node.source_asset_id and node.source_version_id for node in nodes)

    relations = cooccurrence_relations(documents, nodes, max_relations=20)
    assert relations
    assert relations[0]["count"] >= 1
    assert relations[0]["evidence"][0]["asset_version_id"] in {"kav_1", "kav_2"}


def test_score_document_weights_title_more_than_body() -> None:
    title_hit = DocumentCandidate(
        asset_id="a",
        asset_version_id="v1",
        title="红树林修复",
        text="普通说明",
        asset_type="document",
        source_uri=None,
    )
    body_hit = DocumentCandidate(
        asset_id="b",
        asset_version_id="v2",
        title="普通说明",
        text="红树林修复",
        asset_type="document",
        source_uri=None,
    )

    title_score, title_snippet = score_document("红树林修复", title_hit)
    body_score, body_snippet = score_document("红树林修复", body_hit)
    assert title_score > body_score
    assert "红树林" in title_snippet
    assert "红树林" in body_snippet


def _relation(
    relation_id: str,
    source: str,
    target: str,
    *,
    status: str = OntologyReviewStatus.APPROVED,
    ontology_version_id: str = "ont_v1",
) -> OntologyRelation:
    return OntologyRelation(
        relation_id=relation_id,
        ontology_version_id=ontology_version_id,
        source_node_id=source,
        target_node_id=target,
        relation_type="co_occurs_with",
        description="test",
        properties_json={},
        evidence_json=[],
        confidence=0.8,
        review_status=status,
    )


def test_relation_paths_supports_three_hop_paths_and_skips_unapproved_edges() -> None:
    paths = relation_paths(
        {"a"},
        [
            _relation("r1", "a", "b"),
            _relation("r2", "b", "c"),
            _relation("r3", "c", "d"),
            _relation("r4", "d", "e", status=OntologyReviewStatus.PROPOSED),
        ],
        hop_depth=3,
    )

    assert [step["relation_id"] for step in paths["b"]] == ["r1"]
    assert [step["relation_id"] for step in paths["c"]] == ["r1", "r2"]
    assert [step["relation_id"] for step in paths["d"]] == ["r1", "r2", "r3"]
    assert "e" not in paths


@pytest_asyncio.fixture
async def knowledge_session() -> AsyncSession:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=KNOWLEDGE_TABLES,
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_asset_versions_are_hashed_and_superseded(
    knowledge_session: AsyncSession,
) -> None:
    service = KnowledgeService(knowledge_session)
    asset, first = await service.create_asset(
        KnowledgeAssetCreate(
            asset_type=KnowledgeAssetType.DOCUMENT,
            title="海洋治理政策",
            township="马鼻镇",
            initial_content=KnowledgeAssetVersionCreate(
                content_text="第一版政策正文",
                content_json={"section": 1},
            ),
        ),
        username="operator-a",
    )
    assert asset.current_version == 1
    assert first.content_hash == content_digest(
        "第一版政策正文",
        {"section": 1},
    )

    second = await service.append_asset_version(
        asset.asset_id,
        KnowledgeAssetVersionCreate(content_text="第二版政策正文"),
        username="operator-a",
    )
    await knowledge_session.refresh(first)
    assert second.version_no == 2
    assert asset.current_version == 2
    assert first.status == KnowledgeVersionStatus.SUPERSEDED
    assert second.status == KnowledgeVersionStatus.ACTIVE


@pytest.mark.asyncio
async def test_ontology_candidates_cannot_publish_before_human_review(
    knowledge_session: AsyncSession,
) -> None:
    service = KnowledgeService(knowledge_session)
    _asset, version = await service.create_asset(
        KnowledgeAssetCreate(
            asset_type=KnowledgeAssetType.DOCUMENT,
            title="红树林生态修复规范",
            initial_content=KnowledgeAssetVersionCreate(
                content_text=(
                    "红树林生态修复需要监测水质。"
                    "红树林生态修复需要保留潮沟。"
                    "水质监测结果应纳入修复评估。"
                )
            ),
        ),
        username="operator-a",
    )
    ontology = await service.create_ontology_version(
        OntologyVersionCreate(name="marine-domain"),
        username="operator-a",
    )

    extracted, nodes, relations, node_count, relation_count = (
        await service.extract_ontology(
            OntologyExtractRequest(
                ontology_version_id=ontology.version_id,
                asset_version_ids=[version.version_id],
                max_nodes=20,
                max_relations=40,
            ),
            username="operator-a",
        )
    )
    assert extracted.status == OntologyVersionStatus.IN_REVIEW
    assert node_count == len(nodes) > 0
    assert relation_count == len(relations)
    assert all(node.review_status == OntologyReviewStatus.PROPOSED for node in nodes)

    with pytest.raises(AppException) as exc_info:
        await service.publish_ontology(ontology.version_id, username="approver")
    assert exc_info.value.code == ONTOLOGY_NOT_PUBLISHABLE

    for node in nodes:
        await service.review_node(
            node.node_id,
            OntologyReviewStatus.APPROVED,
            username="approver",
            reason="matches domain standard",
        )
    for relation in relations:
        await service.review_relation(
            relation.relation_id,
            OntologyReviewStatus.APPROVED,
            username="approver",
            reason="evidence verified",
        )

    published, counts = await service.publish_ontology(
        ontology.version_id,
        username="approver",
    )
    assert published.status == OntologyVersionStatus.PUBLISHED
    assert counts["approved_nodes"] == len(nodes)
    assert counts["approved_relations"] == len(relations)


@pytest.mark.asyncio
async def test_ontology_version_list_returns_latest_package_first(
    knowledge_session: AsyncSession,
) -> None:
    service = KnowledgeService(knowledge_session)
    first = await service.create_ontology_version(
        OntologyVersionCreate(name="policy-domain"),
        username="operator-a",
    )
    second = await service.create_ontology_version(
        OntologyVersionCreate(
            name="policy-domain",
            parent_version_id=first.version_id,
        ),
        username="operator-a",
    )

    versions = await service.list_ontology_versions()
    assert [version.version_id for version in versions] == [
        second.version_id,
        first.version_id,
    ]


@pytest.mark.asyncio
async def test_search_and_decision_use_approved_multihop_evidence(
    knowledge_session: AsyncSession,
) -> None:
    service = KnowledgeService(knowledge_session)
    source_asset, source_version = await service.create_asset(
        KnowledgeAssetCreate(
            asset_type=KnowledgeAssetType.DOCUMENT,
            title="红树林巡护记录",
            township="马鼻镇",
            initial_content=KnowledgeAssetVersionCreate(
                content_text="红树林区域发现垃圾聚集，需要安排清理。"
            ),
        ),
        username="operator-a",
    )
    bridge_asset, bridge_version = await service.create_asset(
        KnowledgeAssetCreate(
            asset_type=KnowledgeAssetType.TABLE,
            title="生态影响统计",
            township="马鼻镇",
            initial_content=KnowledgeAssetVersionCreate(
                content_json={"metric": "清理优先级", "value": 5}
            ),
        ),
        username="operator-a",
    )
    target_asset, target_version = await service.create_asset(
        KnowledgeAssetCreate(
            asset_type=KnowledgeAssetType.DOCUMENT,
            title="处置预案",
            township="马鼻镇",
            initial_content=KnowledgeAssetVersionCreate(
                content_text="处置预案规定响应等级和人员调度要求。"
            ),
        ),
        username="operator-a",
    )
    ontology = OntologyVersion(
        version_id="ont_test",
        name="test-domain",
        version_no=1,
        status=OntologyVersionStatus.PUBLISHED,
        standard_codes=[],
        metadata_json={},
        created_by="approver",
    )
    nodes = [
        OntologyNode(
            node_id="nod_a",
            ontology_version_id=ontology.version_id,
            entity_type="concept",
            name="红树林",
            canonical_name="红树林",
            aliases=[],
            properties_json={},
            source_asset_id=source_asset.asset_id,
            source_version_id=source_version.version_id,
            confidence=0.9,
            review_status=OntologyReviewStatus.APPROVED,
        ),
        OntologyNode(
            node_id="nod_b",
            ontology_version_id=ontology.version_id,
            entity_type="metric",
            name="清理优先级",
            canonical_name="清理优先级",
            aliases=[],
            properties_json={},
            source_asset_id=bridge_asset.asset_id,
            source_version_id=bridge_version.version_id,
            confidence=0.9,
            review_status=OntologyReviewStatus.APPROVED,
        ),
        OntologyNode(
            node_id="nod_c",
            ontology_version_id=ontology.version_id,
            entity_type="concept",
            name="响应等级",
            canonical_name="响应等级",
            aliases=[],
            properties_json={},
            source_asset_id=target_asset.asset_id,
            source_version_id=target_version.version_id,
            confidence=0.9,
            review_status=OntologyReviewStatus.APPROVED,
        ),
    ]
    relations = [
        _relation(
            "rel_ab",
            "nod_a",
            "nod_b",
            ontology_version_id=ontology.version_id,
        ),
        _relation(
            "rel_bc",
            "nod_b",
            "nod_c",
            ontology_version_id=ontology.version_id,
        ),
    ]
    knowledge_session.add_all([ontology, *nodes, *relations])
    await knowledge_session.flush()

    mode, ontology_version_id, hits = await service.search(
        KnowledgeSearchRequest(
            query="红树林",
            ontology_version_id=ontology.version_id,
            hop_depth=2,
            limit=10,
        ),
        township_scope="马鼻镇",
    )
    assert mode == "ontology_graph"
    assert ontology_version_id == ontology.version_id
    assert [hit.asset_id for hit in hits] == [
        source_asset.asset_id,
        bridge_asset.asset_id,
        target_asset.asset_id,
    ]
    target_hit = hits[2]
    assert target_hit.hop_count == 2
    assert set(target_hit.matched_relation_ids) == {"rel_ab", "rel_bc"}
    assert "红树林" in target_hit.citations[1]

    trace = await service.create_decision(
        DecisionCreate(
            question="红树林垃圾如何处置？",
            ontology_version_id=ontology.version_id,
            hop_depth=2,
        ),
        username="operator-a",
        township_scope="马鼻镇",
    )
    _stored_trace, evidence = await service.get_decision(trace.trace_id)
    assert [item.rank_no for item in evidence] == [1, 2, 3]
    assert [item.asset_id for item in evidence] == [
        source_asset.asset_id,
        bridge_asset.asset_id,
        target_asset.asset_id,
    ]
    assert "search_mode" in trace.metadata_json
    assert json.loads(json.dumps(trace.metadata_json))["search_mode"] == "ontology_graph"


@pytest.mark.asyncio
async def test_extract_and_decision_reject_cross_township_inputs(
    knowledge_session: AsyncSession,
) -> None:
    service = KnowledgeService(knowledge_session)
    asset, version = await service.create_asset(
        KnowledgeAssetCreate(
            asset_type=KnowledgeAssetType.DOCUMENT,
            title="马鼻镇湿地资料",
            township="马鼻镇",
            initial_content=KnowledgeAssetVersionCreate(content_text="湿地保护范围"),
        ),
        username="operator-a",
    )
    ontology = await service.create_ontology_version(
        OntologyVersionCreate(name="scope-test"),
        username="operator-b",
    )

    with pytest.raises(AppException) as extract_error:
        await service.extract_ontology(
            OntologyExtractRequest(
                ontology_version_id=ontology.version_id,
                asset_version_ids=[version.version_id],
            ),
            username="operator-b",
            township_scope="苔菉镇",
        )
    assert extract_error.value.code == 1004

    with pytest.raises(AppException) as decision_error:
        await service.create_decision(
            DecisionCreate(
                question="跨辖区引用测试",
                evidence=[
                    {
                        "asset_id": asset.asset_id,
                        "asset_version_id": version.version_id,
                        "citation_text": "不应允许跨辖区证据",
                    }
                ],
            ),
            username="operator-b",
            township_scope="苔菉镇",
        )
    assert decision_error.value.code == 1004


def _term_candidate(name: str) -> TermCandidate:
    return TermCandidate(
        term=name,
        canonical_name=name,
        score=1.0,
        source_asset_id="ast_relation",
        source_version_id="kav_relation_1",
    )


def test_relation_scores_prefer_close_sentence_local_evidence() -> None:
    nodes = [_term_candidate("岸基摄像头"), _term_candidate("派单")]
    near_document = DocumentCandidate(
        asset_id="ast_near",
        asset_version_id="kav_near_1",
        title="岸基摄像头派单记录",
        text="岸基摄像头派单。",
        asset_type=KnowledgeAssetType.DOCUMENT,
        source_uri=None,
    )
    far_document = DocumentCandidate(
        asset_id="ast_far",
        asset_version_id="kav_far_1",
        title="岸边观测记录",
        text=(
            "岸基摄像头与本期无关的巡检记录和现场说明后，设备仍无异常，"
            + "巡视" * 70
            + "派单。"
        ),
        asset_type=KnowledgeAssetType.DOCUMENT,
        source_uri=None,
    )

    near_relation = cooccurrence_relations(
        [near_document], nodes, max_relations=1
    )[0]
    far_relation = cooccurrence_relations(
        [far_document], nodes, max_relations=1
    )[0]

    assert near_relation["distance"] < far_relation["distance"]
    assert near_relation["score"] > far_relation["score"]


def test_relation_types_and_source_evidence_are_preserved() -> None:
    document = DocumentCandidate(
        asset_id="ast_dispatch",
        asset_version_id="kav_dispatch_1",
        title="海漂垃圾处置规则",
        text="岸基摄像头按照监测结果派单；打捞机器人执行拾取。",
        asset_type=KnowledgeAssetType.DOCUMENT,
        source_uri="policy://dispatch",
    )
    nodes = [
        _term_candidate("岸基摄像头"),
        _term_candidate("派单"),
        _term_candidate("打捞机器人"),
        _term_candidate("拾取"),
    ]

    relations = cooccurrence_relations([document], nodes, max_relations=20)
    by_pair = {
        (relation["source"].canonical_name, relation["target"].canonical_name): relation
        for relation in relations
    }

    assert by_pair[("岸基摄像头", "派单")]["relation_type"] == "monitors"
    assert by_pair[("打捞机器人", "拾取")]["relation_type"] == "executes"
    for relation in relations:
        evidence = relation["evidence"][0]
        assert evidence["asset_id"] == "ast_dispatch"
        assert evidence["asset_version_id"] == "kav_dispatch_1"
        assert evidence["citation"]


def test_generic_operational_words_do_not_rank_as_domain_candidates() -> None:
    documents = [
        DocumentCandidate(
            asset_id=f"ast_generic_{index}",
            asset_version_id=f"kav_generic_{index}",
            title="海漂垃圾处置工作方案",
            text="工作通知要求各单位反馈情况，相关工作由系统平台管理。",
            asset_type=KnowledgeAssetType.DOCUMENT,
            source_uri=None,
        )
        for index in range(2)
    ]
    terms = {
        node.canonical_name
        for node in extract_term_candidates(documents, max_nodes=30, min_term_length=2)
    }

    assert "海漂垃圾" in terms
    assert not ({"通知", "方案", "工作", "系统", "平台"} & terms)


def test_standard_title_relations_cover_dumping_material_types() -> None:
    document = DocumentCandidate(
        asset_id="ast_dumping_standard",
        asset_version_id="kav_dumping_standard_1",
        title=(
            "《海洋倾倒物质评价规范》适用于疏浚物、渔业废料和惰性无机地质材料"
        ),
        text=(
            "《海洋倾倒物质评价规范》适用于疏浚物、渔业废料和惰性无机地质材料。"
        ),
        asset_type=KnowledgeAssetType.DOCUMENT,
        source_uri="https://www.mee.gov.cn/standard",
    )
    nodes = extract_term_candidates([document], max_nodes=30, min_term_length=2)
    node_names = {node.canonical_name for node in nodes}
    assert {
        "海洋倾倒物质评价规范",
        "疏浚物",
        "渔业废料",
        "惰性无机地质材料",
    } <= node_names

    relations = cooccurrence_relations([document], nodes, max_relations=30)
    relation_pairs = {
        (relation["source"].canonical_name, relation["target"].canonical_name)
        for relation in relations
    }
    for material in ("疏浚物", "渔业废料", "惰性无机地质材料"):
        assert ("海洋倾倒物质评价规范", material) in relation_pairs
