"""HTTP contracts for knowledge assets, ontology evolution, and evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas import PageMeta

AssetTypeLiteral = Literal["document", "table", "image", "event", "telemetry", "dataset"]
AssetStatusLiteral = Literal["draft", "active", "archived"]
OntologyStatusLiteral = Literal["draft", "in_review", "published", "retired"]
ReviewDecisionLiteral = Literal["approved", "rejected"]


class KnowledgeAssetCreate(BaseModel):
    """Register a stable asset identity."""

    asset_id: str | None = Field(default=None, max_length=64)
    asset_type: AssetTypeLiteral
    title: str = Field(..., min_length=1, max_length=256)
    description: str | None = Field(default=None, max_length=4000)
    source_uri: str | None = Field(default=None, max_length=1024)
    source_system: str | None = Field(default=None, max_length=128)
    mime_type: str | None = Field(default=None, max_length=128)
    region: str | None = Field(default=None, max_length=128)
    township: str | None = Field(default=None, max_length=64)
    security_level: Literal["public", "internal", "restricted"] = "internal"
    status: AssetStatusLiteral = "active"
    standard_codes: list[str] = Field(default_factory=list, max_length=64)
    tags: list[str] = Field(default_factory=list, max_length=64)
    attributes: dict[str, Any] = Field(default_factory=dict)
    initial_content: "KnowledgeAssetVersionCreate | None" = None


class KnowledgeAssetVersionCreate(BaseModel):
    """Append a version snapshot to an existing asset."""

    content_text: str | None = Field(default=None, max_length=2_000_000)
    content_json: dict[str, Any] | list[Any] | None = None
    extraction_method: str = Field(default="manual", min_length=1, max_length=64)
    extraction_confidence: float | None = Field(default=None, ge=0, le=1)
    language: str | None = Field(default=None, max_length=32)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeAssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    asset_id: str
    asset_type: str
    title: str
    description: str | None = None
    source_uri: str | None = None
    source_system: str | None = None
    mime_type: str | None = None
    region: str | None = None
    township: str | None = None
    security_level: str
    status: str
    current_version: int
    standard_codes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class KnowledgeAssetVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version_id: str
    asset_id: str
    version_no: int
    status: str
    content_hash: str
    content_text: str | None = None
    content_json: dict[str, Any] | list[Any] | None = None
    extraction_method: str
    extraction_confidence: float | None = None
    language: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: datetime | None = None


class KnowledgeAssetDetailOut(BaseModel):
    asset: KnowledgeAssetOut
    versions: list[KnowledgeAssetVersionOut] = Field(default_factory=list)


class KnowledgeAssetPageOut(BaseModel):
    items: list[KnowledgeAssetOut] = Field(default_factory=list)
    meta: PageMeta = Field(default_factory=PageMeta)


class OntologyVersionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    version_no: int | None = Field(default=None, ge=1)
    parent_version_id: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=4000)
    standard_codes: list[str] = Field(default_factory=list, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)


class OntologyVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version_id: str
    name: str
    version_no: int
    status: str
    parent_version_id: str | None = None
    description: str | None = None
    standard_codes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    published_by: str | None = None
    published_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class OntologyExtractRequest(BaseModel):
    """Run deterministic candidate extraction into one ontology version."""

    ontology_version_id: str = Field(..., min_length=1, max_length=64)
    asset_version_ids: list[str] = Field(..., min_length=1, max_length=50)
    max_nodes: int = Field(default=20, ge=1, le=100)
    max_relations: int = Field(default=40, ge=0, le=200)
    min_term_length: int = Field(default=2, ge=2, le=12)


class OntologyNodeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    node_id: str
    ontology_version_id: str
    entity_type: str
    name: str
    canonical_name: str
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    source_asset_id: str | None = None
    source_version_id: str | None = None
    confidence: float
    review_status: str
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime | None = None


class OntologyRelationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    relation_id: str
    ontology_version_id: str
    source_node_id: str
    target_node_id: str
    relation_type: str
    description: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float
    review_status: str
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime | None = None


class OntologyExtractResult(BaseModel):
    version: OntologyVersionOut
    nodes: list[OntologyNodeOut] = Field(default_factory=list)
    relations: list[OntologyRelationOut] = Field(default_factory=list)
    created_nodes: int = 0
    created_relations: int = 0


class OntologyReviewRequest(BaseModel):
    decision: ReviewDecisionLiteral
    reason: str | None = Field(default=None, max_length=1000)


class OntologyPublishResult(BaseModel):
    version: OntologyVersionOut
    approved_nodes: int
    rejected_nodes: int
    approved_relations: int
    rejected_relations: int


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000)
    ontology_version_id: str | None = Field(default=None, max_length=64)
    hop_depth: int = Field(default=2, ge=1, le=3)
    asset_types: list[AssetTypeLiteral] = Field(default_factory=list, max_length=6)
    standard_codes: list[str] = Field(default_factory=list, max_length=32)
    limit: int = Field(default=10, ge=1, le=50)


class KnowledgePathStep(BaseModel):
    hop_no: int
    source_node_id: str | None = None
    relation_id: str | None = None
    target_node_id: str | None = None
    relation_type: str | None = None


class KnowledgeSearchHit(BaseModel):
    asset_id: str
    asset_version_id: str
    title: str
    asset_type: str
    source_uri: str | None = None
    score: float
    snippet: str
    matched_node_ids: list[str] = Field(default_factory=list)
    matched_relation_ids: list[str] = Field(default_factory=list)
    hop_count: int = 0
    path: list[KnowledgePathStep] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)


class KnowledgeSearchOut(BaseModel):
    query: str
    mode: str
    ontology_version_id: str | None = None
    total: int
    results: list[KnowledgeSearchHit] = Field(default_factory=list)


class DecisionEvidenceIn(BaseModel):
    asset_id: str | None = Field(default=None, max_length=64)
    asset_version_id: str | None = Field(default=None, max_length=64)
    node_id: str | None = Field(default=None, max_length=64)
    relation_id: str | None = Field(default=None, max_length=64)
    hop_no: int = Field(default=0, ge=0, le=10)
    citation_text: str = Field(..., min_length=1, max_length=4000)
    source_uri: str | None = Field(default=None, max_length=1024)
    score: float = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DecisionCreate(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    answer_summary: str | None = Field(default=None, max_length=8000)
    run_id: str | None = Field(default=None, max_length=64)
    ontology_version_id: str | None = Field(default=None, max_length=64)
    policy_version: str | None = Field(default=None, max_length=64)
    hop_depth: int = Field(default=2, ge=1, le=3)
    evidence: list[DecisionEvidenceIn] = Field(default_factory=list, max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DecisionEvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    evidence_id: str
    trace_id: str
    rank_no: int
    asset_id: str | None = None
    asset_version_id: str | None = None
    node_id: str | None = None
    relation_id: str | None = None
    hop_no: int
    citation_text: str
    source_uri: str | None = None
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class DecisionTraceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trace_id: str
    run_id: str | None = None
    question: str
    answer_summary: str | None = None
    status: str
    ontology_version_id: str | None = None
    policy_version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by: str
    created_at: datetime | None = None
    evidence: list[DecisionEvidenceOut] = Field(default_factory=list)


class DecisionTracePageOut(BaseModel):
    items: list[DecisionTraceOut] = Field(default_factory=list)
    meta: PageMeta = Field(default_factory=PageMeta)


KnowledgeAssetCreate.model_rebuild()

