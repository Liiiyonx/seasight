"""Knowledge asset, evolving ontology, search, and decision-trace API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser, get_current_user, require_operator
from app.core.exceptions import ApiResponse, AppException, ErrorCode
from app.db.session import get_session
from app.models.knowledge import (
    DecisionEvidence,
    DecisionTrace,
    KnowledgeAsset,
    KnowledgeAssetVersion,
    OntologyNode,
    OntologyRelation,
    OntologyVersion,
)
from app.schemas import (
    DecisionCreate,
    DecisionEvidenceOut,
    DecisionTraceOut,
    DecisionTracePageOut,
    KnowledgeAssetCreate,
    KnowledgeAssetDetailOut,
    KnowledgeAssetOut,
    KnowledgeAssetPageOut,
    KnowledgeAssetVersionCreate,
    KnowledgeAssetVersionOut,
    KnowledgePathStep,
    KnowledgeSearchHit,
    KnowledgeSearchOut,
    KnowledgeSearchRequest,
    OntologyExtractRequest,
    OntologyExtractResult,
    OntologyNodeOut,
    OntologyPublishResult,
    OntologyRelationOut,
    OntologyReviewRequest,
    OntologyVersionCreate,
    OntologyVersionOut,
    PageMeta,
)
from app.services.audit import record_audit
from app.services.knowledge import (
    ASSET_NOT_FOUND,
    DECISION_TRACE_NOT_FOUND,
    KnowledgeService,
)

router = APIRouter()

ONTOLOGY_REVIEW_ROLES = ("admin", "approver")


async def _require_ontology_reviewer(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    """Ontology review and publication are approval-class operations."""
    if not (user.is_admin or user.role in ONTOLOGY_REVIEW_ROLES):
        raise AppException(
            code=ErrorCode.FORBIDDEN,
            message="当前账号无本体审核权限（需要 admin 或 approver）",
            http_status=403,
        )
    return user


def _asset_out(asset: KnowledgeAsset) -> KnowledgeAssetOut:
    return KnowledgeAssetOut(
        asset_id=asset.asset_id,
        asset_type=asset.asset_type,
        title=asset.title,
        description=asset.description,
        source_uri=asset.source_uri,
        source_system=asset.source_system,
        mime_type=asset.mime_type,
        region=asset.region,
        township=asset.township,
        security_level=asset.security_level,
        status=asset.status,
        current_version=asset.current_version,
        standard_codes=list(asset.standard_codes or []),
        tags=list(asset.tags or []),
        attributes=dict(asset.attributes_json or {}),
        created_by=asset.created_by,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


def _version_out(version: KnowledgeAssetVersion) -> KnowledgeAssetVersionOut:
    return KnowledgeAssetVersionOut(
        version_id=version.version_id,
        asset_id=version.asset_id,
        version_no=version.version_no,
        status=version.status,
        content_hash=version.content_hash,
        content_text=version.content_text,
        content_json=version.content_json,
        extraction_method=version.extraction_method,
        extraction_confidence=(
            float(version.extraction_confidence)
            if version.extraction_confidence is not None
            else None
        ),
        language=version.language,
        valid_from=version.valid_from,
        valid_to=version.valid_to,
        metadata=dict(version.metadata_json or {}),
        created_by=version.created_by,
        created_at=version.created_at,
    )


def _ontology_out(version: OntologyVersion) -> OntologyVersionOut:
    return OntologyVersionOut(
        version_id=version.version_id,
        name=version.name,
        version_no=version.version_no,
        status=version.status,
        parent_version_id=version.parent_version_id,
        description=version.description,
        standard_codes=list(version.standard_codes or []),
        metadata=dict(version.metadata_json or {}),
        created_by=version.created_by,
        reviewed_by=version.reviewed_by,
        reviewed_at=version.reviewed_at,
        published_by=version.published_by,
        published_at=version.published_at,
        created_at=version.created_at,
        updated_at=version.updated_at,
    )


def _node_out(node: OntologyNode) -> OntologyNodeOut:
    return OntologyNodeOut(
        node_id=node.node_id,
        ontology_version_id=node.ontology_version_id,
        entity_type=node.entity_type,
        name=node.name,
        canonical_name=node.canonical_name,
        description=node.description,
        aliases=list(node.aliases or []),
        properties=dict(node.properties_json or {}),
        source_asset_id=node.source_asset_id,
        source_version_id=node.source_version_id,
        confidence=float(node.confidence),
        review_status=node.review_status,
        reviewed_by=node.reviewed_by,
        reviewed_at=node.reviewed_at,
        created_at=node.created_at,
    )


def _relation_out(relation: OntologyRelation) -> OntologyRelationOut:
    return OntologyRelationOut(
        relation_id=relation.relation_id,
        ontology_version_id=relation.ontology_version_id,
        source_node_id=relation.source_node_id,
        target_node_id=relation.target_node_id,
        relation_type=relation.relation_type,
        description=relation.description,
        properties=dict(relation.properties_json or {}),
        evidence=list(relation.evidence_json or []),
        confidence=float(relation.confidence),
        review_status=relation.review_status,
        reviewed_by=relation.reviewed_by,
        reviewed_at=relation.reviewed_at,
        created_at=relation.created_at,
    )


def _evidence_out(evidence: DecisionEvidence) -> DecisionEvidenceOut:
    return DecisionEvidenceOut(
        evidence_id=evidence.evidence_id,
        trace_id=evidence.trace_id,
        rank_no=evidence.rank_no,
        asset_id=evidence.asset_id,
        asset_version_id=evidence.asset_version_id,
        node_id=evidence.node_id,
        relation_id=evidence.relation_id,
        hop_no=evidence.hop_no,
        citation_text=evidence.citation_text,
        source_uri=evidence.source_uri,
        score=float(evidence.score),
        metadata=dict(evidence.metadata_json or {}),
        created_at=evidence.created_at,
    )


def _trace_out(
    trace: DecisionTrace,
    evidence: list[DecisionEvidence] | None = None,
) -> DecisionTraceOut:
    return DecisionTraceOut(
        trace_id=trace.trace_id,
        run_id=trace.run_id,
        question=trace.question,
        answer_summary=trace.answer_summary,
        status=trace.status,
        ontology_version_id=trace.ontology_version_id,
        policy_version=trace.policy_version,
        metadata=dict(trace.metadata_json or {}),
        created_by=trace.created_by,
        created_at=trace.created_at,
        evidence=[_evidence_out(item) for item in (evidence or [])],
    )


def _ensure_asset_scope(asset: KnowledgeAsset, user: CurrentUser) -> None:
    if user.role != "operator" or not user.township_scope:
        return
    if asset.township != user.township_scope:
        raise AppException(
            code=ErrorCode.FORBIDDEN,
            message="当前账号无权访问其他辖区知识资产",
            http_status=403,
        )


@router.post(
    "/assets",
    response_model=ApiResponse[KnowledgeAssetDetailOut],
    summary="登记多模态知识资产",
)
async def create_asset(
    payload: KnowledgeAssetCreate,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_operator),
):
    service = KnowledgeService(session)
    asset, version = await service.create_asset(
        payload,
        username=user.username,
        township_scope=user.township_scope if user.role == "operator" else None,
    )
    await record_audit(
        session,
        username=user.username,
        role=user.role,
        action="knowledge_asset_create",
        target_type="knowledge_asset",
        target_id=asset.asset_id,
        detail=f"type={asset.asset_type} version={version.version_id}",
    )
    return ApiResponse.ok(
        KnowledgeAssetDetailOut(asset=_asset_out(asset), versions=[_version_out(version)]),
        message="知识资产已登记",
    )


@router.get(
    "/assets",
    response_model=ApiResponse[KnowledgeAssetPageOut],
    summary="知识资产列表",
)
async def list_assets(
    asset_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    region: str | None = Query(default=None),
    query: str | None = Query(default=None, max_length=256),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    township = user.township_scope if user.role == "operator" else None
    items, total = await KnowledgeService(session).list_assets(
        asset_type=asset_type,
        status=status,
        region=region,
        township=township,
        query=query,
        page=page,
        page_size=page_size,
    )
    return ApiResponse.ok(
        KnowledgeAssetPageOut(
            items=[_asset_out(item) for item in items],
            meta=PageMeta(total=total, page=page, page_size=page_size),
        )
    )


@router.get(
    "/assets/{asset_id}",
    response_model=ApiResponse[KnowledgeAssetDetailOut],
    summary="知识资产详情与版本",
)
async def get_asset(
    asset_id: str,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    asset, versions = await KnowledgeService(session).get_asset(asset_id)
    _ensure_asset_scope(asset, user)
    return ApiResponse.ok(
        KnowledgeAssetDetailOut(
            asset=_asset_out(asset),
            versions=[_version_out(version) for version in versions],
        )
    )


@router.post(
    "/assets/{asset_id}/versions",
    response_model=ApiResponse[KnowledgeAssetVersionOut],
    summary="追加知识资产版本",
)
async def append_asset_version(
    asset_id: str,
    payload: KnowledgeAssetVersionCreate,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_operator),
):
    asset, _versions = await KnowledgeService(session).get_asset(asset_id)
    _ensure_asset_scope(asset, user)
    version = await KnowledgeService(session).append_asset_version(
        asset_id,
        payload,
        username=user.username,
    )
    await record_audit(
        session,
        username=user.username,
        role=user.role,
        action="knowledge_asset_version_create",
        target_type="knowledge_asset",
        target_id=asset_id,
        detail=f"version={version.version_id} no={version.version_no}",
    )
    return ApiResponse.ok(_version_out(version), message="知识资产版本已追加")


@router.post(
    "/ontology/versions",
    response_model=ApiResponse[OntologyVersionOut],
    summary="创建本体版本",
)
async def create_ontology_version(
    payload: OntologyVersionCreate,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_operator),
):
    version = await KnowledgeService(session).create_ontology_version(
        payload,
        username=user.username,
    )
    await record_audit(
        session,
        username=user.username,
        role=user.role,
        action="ontology_version_create",
        target_type="ontology_version",
        target_id=version.version_id,
        detail=f"{version.name} v{version.version_no}",
    )
    return ApiResponse.ok(_ontology_out(version), message="本体版本已创建")


@router.get(
    "/ontology/versions",
    response_model=ApiResponse[list[OntologyVersionOut]],
    summary="本体版本列表",
)
async def list_ontology_versions(
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
):
    versions = await KnowledgeService(session).list_ontology_versions()
    return ApiResponse.ok([_ontology_out(version) for version in versions])


@router.get(
    "/ontology/versions/{version_id}",
    response_model=ApiResponse[OntologyVersionOut],
    summary="本体版本详情",
)
async def get_ontology_version(
    version_id: str,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
):
    version = await KnowledgeService(session).get_ontology_version(version_id)
    return ApiResponse.ok(_ontology_out(version))


@router.get(
    "/ontology/versions/{version_id}/nodes",
    response_model=ApiResponse[list[OntologyNodeOut]],
    summary="本体节点列表",
)
async def list_ontology_nodes(
    version_id: str,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
):
    nodes = await KnowledgeService(session).list_ontology_nodes(version_id)
    return ApiResponse.ok([_node_out(node) for node in nodes])


@router.get(
    "/ontology/versions/{version_id}/relations",
    response_model=ApiResponse[list[OntologyRelationOut]],
    summary="本体关系列表",
)
async def list_ontology_relations(
    version_id: str,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
):
    relations = await KnowledgeService(session).list_ontology_relations(version_id)
    return ApiResponse.ok([_relation_out(relation) for relation in relations])


@router.post(
    "/ontology/extract",
    response_model=ApiResponse[OntologyExtractResult],
    summary="从资产半自动抽取本体候选",
)
async def extract_ontology(
    payload: OntologyExtractRequest,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_operator),
):
    version, nodes, relations, node_count, relation_count = (
        await KnowledgeService(session).extract_ontology(
            payload,
            username=user.username,
            township_scope=user.township_scope if user.role == "operator" else None,
        )
    )
    await record_audit(
        session,
        username=user.username,
        role=user.role,
        action="ontology_extract",
        target_type="ontology_version",
        target_id=version.version_id,
        detail=f"nodes={node_count} relations={relation_count}",
    )
    return ApiResponse.ok(
        OntologyExtractResult(
            version=_ontology_out(version),
            nodes=[_node_out(node) for node in nodes],
            relations=[_relation_out(relation) for relation in relations],
            created_nodes=node_count,
            created_relations=relation_count,
        ),
        message="本体候选已生成，待人工审核",
    )


@router.post(
    "/ontology/nodes/{node_id}/review",
    response_model=ApiResponse[OntologyNodeOut],
    summary="审核本体节点",
)
async def review_ontology_node(
    node_id: str,
    payload: OntologyReviewRequest,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(_require_ontology_reviewer),
):
    node = await KnowledgeService(session).review_node(
        node_id,
        payload.decision,
        username=user.username,
        reason=payload.reason,
    )
    await record_audit(
        session,
        username=user.username,
        role=user.role,
        action="ontology_node_review",
        target_type="ontology_node",
        target_id=node_id,
        detail=f"decision={payload.decision} reason={payload.reason or ''}",
    )
    return ApiResponse.ok(_node_out(node), message="本体节点审核已记录")


@router.post(
    "/ontology/relations/{relation_id}/review",
    response_model=ApiResponse[OntologyRelationOut],
    summary="审核本体关系",
)
async def review_ontology_relation(
    relation_id: str,
    payload: OntologyReviewRequest,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(_require_ontology_reviewer),
):
    relation = await KnowledgeService(session).review_relation(
        relation_id,
        payload.decision,
        username=user.username,
        reason=payload.reason,
    )
    await record_audit(
        session,
        username=user.username,
        role=user.role,
        action="ontology_relation_review",
        target_type="ontology_relation",
        target_id=relation_id,
        detail=f"decision={payload.decision} reason={payload.reason or ''}",
    )
    return ApiResponse.ok(_relation_out(relation), message="本体关系审核已记录")


@router.post(
    "/ontology/versions/{version_id}/publish",
    response_model=ApiResponse[OntologyPublishResult],
    summary="发布本体版本",
)
async def publish_ontology_version(
    version_id: str,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(_require_ontology_reviewer),
):
    version, counts = await KnowledgeService(session).publish_ontology(
        version_id,
        username=user.username,
    )
    await record_audit(
        session,
        username=user.username,
        role=user.role,
        action="ontology_version_publish",
        target_type="ontology_version",
        target_id=version_id,
        detail=",".join(f"{key}={value}" for key, value in counts.items()),
    )
    return ApiResponse.ok(
        OntologyPublishResult(version=_ontology_out(version), **counts),
        message="本体版本已发布",
    )


@router.post(
    "/search",
    response_model=ApiResponse[KnowledgeSearchOut],
    summary="跨文档多跳知识检索",
)
async def search_knowledge(
    payload: KnowledgeSearchRequest,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
):
    mode, ontology_version_id, hits = await KnowledgeService(session).search(
        payload,
        township_scope=user.township_scope if user.role == "operator" else None,
    )
    return ApiResponse.ok(
        KnowledgeSearchOut(
            query=payload.query,
            mode=mode,
            ontology_version_id=ontology_version_id,
            total=len(hits),
            results=[
                KnowledgeSearchHit(
                    **{
                        **hit.__dict__,
                        "path": [KnowledgePathStep(**step) for step in hit.path],
                    }
                )
                for hit in hits
            ],
        )
    )


@router.post(
    "/decisions",
    response_model=ApiResponse[DecisionTraceOut],
    summary="记录可溯源的决策轨迹",
)
async def create_decision(
    payload: DecisionCreate,
    session: AsyncSession = Depends(get_session),
    user: CurrentUser = Depends(require_operator),
):
    service = KnowledgeService(session)
    trace = await service.create_decision(
        payload,
        username=user.username,
        township_scope=user.township_scope if user.role == "operator" else None,
    )
    _trace, evidence = await service.get_decision(trace.trace_id)
    await record_audit(
        session,
        username=user.username,
        role=user.role,
        action="decision_trace_create",
        target_type="decision_trace",
        target_id=trace.trace_id,
        detail=f"status={trace.status} evidence={len(evidence)}",
    )
    return ApiResponse.ok(
        _trace_out(trace, evidence),
        message="决策轨迹已记录" if evidence else "证据不足，未形成可引用决策",
    )


@router.get(
    "/decisions",
    response_model=ApiResponse[DecisionTracePageOut],
    summary="决策轨迹列表",
)
async def list_decisions(
    run_id: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
):
    traces, total = await KnowledgeService(session).list_decisions(
        run_id=run_id,
        page=page,
        page_size=page_size,
    )
    return ApiResponse.ok(
        DecisionTracePageOut(
            items=[_trace_out(trace) for trace in traces],
            meta=PageMeta(total=total, page=page, page_size=page_size),
        )
    )


@router.get(
    "/decisions/{trace_id}/evidence",
    response_model=ApiResponse[list[DecisionEvidenceOut]],
    summary="决策证据链",
)
async def get_decision_evidence(
    trace_id: str,
    session: AsyncSession = Depends(get_session),
    _user: CurrentUser = Depends(get_current_user),
):
    _trace, evidence = await KnowledgeService(session).get_decision(trace_id)
    return ApiResponse.ok([_evidence_out(item) for item in evidence])
