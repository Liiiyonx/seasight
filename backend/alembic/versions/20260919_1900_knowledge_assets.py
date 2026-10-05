"""knowledge assets, evolving ontology, and decision evidence

Revision ID: 20260919_1900_knowledge_assets
Revises: 20260919_1800_approver_role
Create Date: 2026-09-19 19:00:00

Adds the Huawei/Nexent knowledge domain without changing the robot dispatch
tables.  Fresh installations receive the same tables from
backend/db/init/01_schema.sql before being stamped; existing installations run
this migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260919_1900_knowledge_assets"
down_revision: str | None = "20260919_1800_approver_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    """Create the knowledge and evidence tables."""
    op.create_table(
        "t_knowledge_asset",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("asset_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source_uri", sa.String(length=1024), nullable=True),
        sa.Column("source_system", sa.String(length=128), nullable=True),
        sa.Column("mime_type", sa.String(length=128), nullable=True),
        sa.Column("region", sa.String(length=128), nullable=True),
        sa.Column("township", sa.String(length=64), nullable=True),
        sa.Column(
            "security_level",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'internal'"),
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "current_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("standard_codes", _jsonb(), nullable=False),
        sa.Column("tags", _jsonb(), nullable=False),
        sa.Column("attributes_json", _jsonb(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "asset_type IN ('document','table','image','event','telemetry','dataset')",
            name="chk_knowledge_asset_type",
        ),
        sa.CheckConstraint(
            "status IN ('draft','active','archived')",
            name="chk_knowledge_asset_status",
        ),
        sa.CheckConstraint("current_version >= 1", name="chk_knowledge_asset_version"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id", name="uq_knowledge_asset_asset_id"),
    )
    op.create_index(
        "idx_knowledge_asset_type_status",
        "t_knowledge_asset",
        ["asset_type", "status"],
    )
    op.create_index(
        "idx_knowledge_asset_region",
        "t_knowledge_asset",
        ["region", "township"],
    )
    op.create_index(
        "idx_knowledge_asset_updated",
        "t_knowledge_asset",
        [sa.text("updated_at DESC")],
    )

    op.create_table(
        "t_knowledge_asset_version",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("content_json", _jsonb(), nullable=True),
        sa.Column(
            "extraction_method",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("'manual'"),
        ),
        sa.Column("extraction_confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("language", sa.String(length=32), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata_json", _jsonb(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("version_no >= 1", name="chk_knowledge_version_no"),
        sa.CheckConstraint(
            "status IN ('active','superseded','retired')",
            name="chk_knowledge_version_status",
        ),
        sa.CheckConstraint(
            "extraction_confidence IS NULL "
            "OR (extraction_confidence >= 0 AND extraction_confidence <= 1)",
            name="chk_knowledge_version_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["t_knowledge_asset.asset_id"],
            name="fk_knowledge_version_asset",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", name="uq_knowledge_version_version_id"),
        sa.UniqueConstraint(
            "asset_id",
            "version_no",
            name="uq_knowledge_version_asset_no",
        ),
    )
    op.create_index(
        "idx_knowledge_version_asset",
        "t_knowledge_asset_version",
        ["asset_id", "version_no"],
    )

    op.create_table(
        "t_ontology_version",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("parent_version_id", sa.String(length=64), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("standard_codes", _jsonb(), nullable=False),
        sa.Column("metadata_json", _jsonb(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("reviewed_by", sa.String(length=64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.String(length=64), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("version_no >= 1", name="chk_ontology_version_no"),
        sa.CheckConstraint(
            "status IN ('draft','in_review','published','retired')",
            name="chk_ontology_version_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", name="uq_ontology_version_version_id"),
        sa.UniqueConstraint("name", "version_no", name="uq_ontology_version_name_no"),
    )
    op.create_index(
        "idx_ontology_version_status",
        "t_ontology_version",
        ["name", "status"],
    )

    op.create_table(
        "t_ontology_node",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("ontology_version_id", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("canonical_name", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("aliases", _jsonb(), nullable=False),
        sa.Column("properties_json", _jsonb(), nullable=False),
        sa.Column("source_asset_id", sa.String(length=64), nullable=True),
        sa.Column("source_version_id", sa.String(length=64), nullable=True),
        sa.Column(
            "confidence",
            sa.Numeric(5, 4),
            nullable=False,
            server_default=sa.text("0.5000"),
        ),
        sa.Column(
            "review_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'proposed'"),
        ),
        sa.Column("reviewed_by", sa.String(length=64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="chk_ontology_node_confidence",
        ),
        sa.CheckConstraint(
            "review_status IN ('proposed','approved','rejected')",
            name="chk_ontology_node_review_status",
        ),
        sa.ForeignKeyConstraint(
            ["ontology_version_id"],
            ["t_ontology_version.version_id"],
            name="fk_ontology_node_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_id", name="uq_ontology_node_node_id"),
        sa.UniqueConstraint(
            "ontology_version_id",
            "canonical_name",
            name="uq_ontology_node_version_name",
        ),
    )
    op.create_index("idx_ontology_node_name", "t_ontology_node", ["name"])
    op.create_index(
        "idx_ontology_node_review",
        "t_ontology_node",
        ["ontology_version_id", "review_status"],
    )

    op.create_table(
        "t_ontology_relation",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("relation_id", sa.String(length=64), nullable=False),
        sa.Column("ontology_version_id", sa.String(length=64), nullable=False),
        sa.Column("source_node_id", sa.String(length=64), nullable=False),
        sa.Column("target_node_id", sa.String(length=64), nullable=False),
        sa.Column("relation_type", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("properties_json", _jsonb(), nullable=False),
        sa.Column("evidence_json", _jsonb(), nullable=False),
        sa.Column(
            "confidence",
            sa.Numeric(5, 4),
            nullable=False,
            server_default=sa.text("0.5000"),
        ),
        sa.Column(
            "review_status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'proposed'"),
        ),
        sa.Column("reviewed_by", sa.String(length=64), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="chk_ontology_relation_confidence",
        ),
        sa.CheckConstraint(
            "review_status IN ('proposed','approved','rejected')",
            name="chk_ontology_relation_review_status",
        ),
        sa.CheckConstraint(
            "source_node_id <> target_node_id",
            name="chk_ontology_relation_distinct_nodes",
        ),
        sa.ForeignKeyConstraint(
            ["ontology_version_id"],
            ["t_ontology_version.version_id"],
            name="fk_ontology_relation_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "relation_id",
            name="uq_ontology_relation_relation_id",
        ),
        sa.UniqueConstraint(
            "ontology_version_id",
            "source_node_id",
            "target_node_id",
            "relation_type",
            name="uq_ontology_relation_edge",
        ),
    )
    op.create_index(
        "idx_ontology_relation_source",
        "t_ontology_relation",
        ["ontology_version_id", "source_node_id"],
    )
    op.create_index(
        "idx_ontology_relation_target",
        "t_ontology_relation",
        ["ontology_version_id", "target_node_id"],
    )

    op.create_table(
        "t_decision_trace",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer_summary", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'completed'"),
        ),
        sa.Column("ontology_version_id", sa.String(length=64), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=True),
        sa.Column("metadata_json", _jsonb(), nullable=False),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('completed','insufficient_evidence','failed')",
            name="chk_decision_trace_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trace_id", name="uq_decision_trace_trace_id"),
    )
    op.create_index("idx_decision_trace_run", "t_decision_trace", ["run_id"])
    op.create_index(
        "idx_decision_trace_created",
        "t_decision_trace",
        [sa.text("created_at DESC")],
    )

    op.create_table(
        "t_decision_evidence",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("evidence_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("rank_no", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.String(length=64), nullable=True),
        sa.Column("asset_version_id", sa.String(length=64), nullable=True),
        sa.Column("node_id", sa.String(length=64), nullable=True),
        sa.Column("relation_id", sa.String(length=64), nullable=True),
        sa.Column("hop_no", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("citation_text", sa.Text(), nullable=False),
        sa.Column("source_uri", sa.String(length=1024), nullable=True),
        sa.Column(
            "score",
            sa.Numeric(10, 6),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("metadata_json", _jsonb(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("rank_no >= 1", name="chk_decision_evidence_rank"),
        sa.CheckConstraint("hop_no >= 0", name="chk_decision_evidence_hop"),
        sa.ForeignKeyConstraint(
            ["trace_id"],
            ["t_decision_trace.trace_id"],
            name="fk_decision_evidence_trace",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "evidence_id",
            name="uq_decision_evidence_evidence_id",
        ),
        sa.UniqueConstraint(
            "trace_id",
            "rank_no",
            name="uq_decision_evidence_trace_rank",
        ),
    )
    op.create_index(
        "idx_decision_evidence_trace",
        "t_decision_evidence",
        ["trace_id", "rank_no"],
    )
    op.create_index(
        "idx_decision_evidence_asset",
        "t_decision_evidence",
        ["asset_id", "asset_version_id"],
    )


def downgrade() -> None:
    """Drop only the knowledge-domain tables."""
    op.drop_table("t_decision_evidence")
    op.drop_table("t_decision_trace")
    op.drop_table("t_ontology_relation")
    op.drop_table("t_ontology_node")
    op.drop_table("t_ontology_version")
    op.drop_table("t_knowledge_asset_version")
    op.drop_table("t_knowledge_asset")

