"""Knowledge asset, evolving ontology, and decision-evidence ORM models.

The knowledge domain is intentionally separate from the robot dispatch tables.
It provides a traceable substrate for heterogeneous industry data:

    t_knowledge_asset          stable asset identity and governance metadata
    t_knowledge_asset_version  immutable content snapshots for each asset
    t_ontology_version         versioned ontology package
    t_ontology_node            reviewed entities in an ontology version
    t_ontology_relation        reviewed graph edges in an ontology version
    t_decision_trace           a decision request and its answer summary
    t_decision_evidence        ordered citations supporting one decision trace

PostgreSQL uses JSONB while SQLite tests use the portable JSON type.  Status
columns use VARCHAR plus CHECK constraints instead of PostgreSQL enums so the
same model metadata can be exercised by lightweight tests.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _json_type():
    """JSONB on PostgreSQL, JSON elsewhere."""
    return JSON().with_variant(JSONB(), "postgresql")


def _pk_type():
    """Big autoincrement primary key with SQLite compatibility."""
    return BigInteger().with_variant(Integer, "sqlite")


class KnowledgeAssetType:
    DOCUMENT = "document"
    TABLE = "table"
    IMAGE = "image"
    EVENT = "event"
    TELEMETRY = "telemetry"
    DATASET = "dataset"

    ALL = (DOCUMENT, TABLE, IMAGE, EVENT, TELEMETRY, DATASET)


class KnowledgeAssetStatus:
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"

    ALL = (DRAFT, ACTIVE, ARCHIVED)


class KnowledgeVersionStatus:
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETIRED = "retired"

    ALL = (ACTIVE, SUPERSEDED, RETIRED)


class OntologyVersionStatus:
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    PUBLISHED = "published"
    RETIRED = "retired"

    ALL = (DRAFT, IN_REVIEW, PUBLISHED, RETIRED)


class OntologyReviewStatus:
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"

    ALL = (PROPOSED, APPROVED, REJECTED)


class DecisionTraceStatus:
    COMPLETED = "completed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    FAILED = "failed"

    ALL = (COMPLETED, INSUFFICIENT_EVIDENCE, FAILED)


class KnowledgeAsset(Base):
    """Stable identity and governance metadata for one industry asset."""

    __tablename__ = "t_knowledge_asset"

    id: Mapped[int] = mapped_column(_pk_type(), primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    source_uri: Mapped[str | None] = mapped_column(String(1024))
    source_system: Mapped[str | None] = mapped_column(String(128))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    region: Mapped[str | None] = mapped_column(String(128))
    township: Mapped[str | None] = mapped_column(String(64))
    security_level: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'internal'")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'active'")
    )
    current_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    standard_codes: Mapped[list[str]] = mapped_column(_json_type(), default=list, nullable=False)
    tags: Mapped[list[str]] = mapped_column(_json_type(), default=list, nullable=False)
    attributes_json: Mapped[dict[str, Any]] = mapped_column(
        _json_type(), default=dict, nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("asset_id", name="uq_knowledge_asset_asset_id"),
        CheckConstraint(
            "asset_type IN ('document','table','image','event','telemetry','dataset')",
            name="chk_knowledge_asset_type",
        ),
        CheckConstraint(
            "status IN ('draft','active','archived')",
            name="chk_knowledge_asset_status",
        ),
        CheckConstraint("current_version >= 1", name="chk_knowledge_asset_version"),
        Index("idx_knowledge_asset_type_status", "asset_type", "status"),
        Index("idx_knowledge_asset_region", "region", "township"),
        Index("idx_knowledge_asset_updated", text("updated_at DESC")),
    )


class KnowledgeAssetVersion(Base):
    """Immutable content snapshot for an asset version."""

    __tablename__ = "t_knowledge_asset_version"

    id: Mapped[int] = mapped_column(_pk_type(), primary_key=True, autoincrement=True)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "t_knowledge_asset.asset_id",
            ondelete="CASCADE",
            name="fk_knowledge_version_asset",
        ),
        nullable=False,
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'active'")
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_text: Mapped[str | None] = mapped_column(Text)
    content_json: Mapped[Any | None] = mapped_column(_json_type())
    extraction_method: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'manual'")
    )
    extraction_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    language: Mapped[str | None] = mapped_column(String(32))
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        _json_type(), default=dict, nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("version_id", name="uq_knowledge_version_version_id"),
        UniqueConstraint("asset_id", "version_no", name="uq_knowledge_version_asset_no"),
        CheckConstraint("version_no >= 1", name="chk_knowledge_version_no"),
        CheckConstraint(
            "status IN ('active','superseded','retired')",
            name="chk_knowledge_version_status",
        ),
        CheckConstraint(
            "extraction_confidence IS NULL "
            "OR (extraction_confidence >= 0 AND extraction_confidence <= 1)",
            name="chk_knowledge_version_confidence",
        ),
        Index("idx_knowledge_version_asset", "asset_id", "version_no"),
    )


class OntologyVersion(Base):
    """Versioned ontology package with explicit review lifecycle."""

    __tablename__ = "t_ontology_version"

    id: Mapped[int] = mapped_column(_pk_type(), primary_key=True, autoincrement=True)
    version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'draft'")
    )
    parent_version_id: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    standard_codes: Mapped[list[str]] = mapped_column(_json_type(), default=list, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        _json_type(), default=dict, nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("version_id", name="uq_ontology_version_version_id"),
        UniqueConstraint("name", "version_no", name="uq_ontology_version_name_no"),
        CheckConstraint("version_no >= 1", name="chk_ontology_version_no"),
        CheckConstraint(
            "status IN ('draft','in_review','published','retired')",
            name="chk_ontology_version_status",
        ),
        Index("idx_ontology_version_status", "name", "status"),
    )


class OntologyNode(Base):
    """Candidate or reviewed entity in an ontology version."""

    __tablename__ = "t_ontology_node"

    id: Mapped[int] = mapped_column(_pk_type(), primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ontology_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "t_ontology_version.version_id",
            ondelete="CASCADE",
            name="fk_ontology_node_version",
        ),
        nullable=False,
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    aliases: Mapped[list[str]] = mapped_column(_json_type(), default=list, nullable=False)
    properties_json: Mapped[dict[str, Any]] = mapped_column(
        _json_type(), default=dict, nullable=False
    )
    source_asset_id: Mapped[str | None] = mapped_column(String(64))
    source_version_id: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, server_default=text("0.5000")
    )
    review_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'proposed'")
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("node_id", name="uq_ontology_node_node_id"),
        UniqueConstraint(
            "ontology_version_id",
            "canonical_name",
            name="uq_ontology_node_version_name",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="chk_ontology_node_confidence",
        ),
        CheckConstraint(
            "review_status IN ('proposed','approved','rejected')",
            name="chk_ontology_node_review_status",
        ),
        Index("idx_ontology_node_name", "name"),
        Index("idx_ontology_node_review", "ontology_version_id", "review_status"),
    )


class OntologyRelation(Base):
    """Candidate or reviewed graph relation in an ontology version."""

    __tablename__ = "t_ontology_relation"

    id: Mapped[int] = mapped_column(_pk_type(), primary_key=True, autoincrement=True)
    relation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ontology_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "t_ontology_version.version_id",
            ondelete="CASCADE",
            name="fk_ontology_relation_version",
        ),
        nullable=False,
    )
    source_node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    properties_json: Mapped[dict[str, Any]] = mapped_column(
        _json_type(), default=dict, nullable=False
    )
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(
        _json_type(), default=list, nullable=False
    )
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, server_default=text("0.5000")
    )
    review_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'proposed'")
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("relation_id", name="uq_ontology_relation_relation_id"),
        UniqueConstraint(
            "ontology_version_id",
            "source_node_id",
            "target_node_id",
            "relation_type",
            name="uq_ontology_relation_edge",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="chk_ontology_relation_confidence",
        ),
        CheckConstraint(
            "review_status IN ('proposed','approved','rejected')",
            name="chk_ontology_relation_review_status",
        ),
        CheckConstraint(
            "source_node_id <> target_node_id",
            name="chk_ontology_relation_distinct_nodes",
        ),
        Index("idx_ontology_relation_source", "ontology_version_id", "source_node_id"),
        Index("idx_ontology_relation_target", "ontology_version_id", "target_node_id"),
    )


class DecisionTrace(Base):
    """One decision request and its summarized answer."""

    __tablename__ = "t_decision_trace"

    id: Mapped[int] = mapped_column(_pk_type(), primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64))
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer_summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'completed'")
    )
    ontology_version_id: Mapped[str | None] = mapped_column(String(64))
    policy_version: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        _json_type(), default=dict, nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("trace_id", name="uq_decision_trace_trace_id"),
        CheckConstraint(
            "status IN ('completed','insufficient_evidence','failed')",
            name="chk_decision_trace_status",
        ),
        Index("idx_decision_trace_run", "run_id"),
        Index("idx_decision_trace_created", text("created_at DESC")),
    )


class DecisionEvidence(Base):
    """Ordered citation supporting a decision trace."""

    __tablename__ = "t_decision_evidence"

    id: Mapped[int] = mapped_column(_pk_type(), primary_key=True, autoincrement=True)
    evidence_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "t_decision_trace.trace_id",
            ondelete="CASCADE",
            name="fk_decision_evidence_trace",
        ),
        nullable=False,
    )
    rank_no: Mapped[int] = mapped_column(Integer, nullable=False)
    asset_id: Mapped[str | None] = mapped_column(String(64))
    asset_version_id: Mapped[str | None] = mapped_column(String(64))
    node_id: Mapped[str | None] = mapped_column(String(64))
    relation_id: Mapped[str | None] = mapped_column(String(64))
    hop_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    citation_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_uri: Mapped[str | None] = mapped_column(String(1024))
    score: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, server_default=text("0")
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        _json_type(), default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("evidence_id", name="uq_decision_evidence_evidence_id"),
        UniqueConstraint("trace_id", "rank_no", name="uq_decision_evidence_trace_rank"),
        CheckConstraint("rank_no >= 1", name="chk_decision_evidence_rank"),
        CheckConstraint("hop_no >= 0", name="chk_decision_evidence_hop"),
        Index("idx_decision_evidence_trace", "trace_id", "rank_no"),
        Index("idx_decision_evidence_asset", "asset_id", "asset_version_id"),
    )
