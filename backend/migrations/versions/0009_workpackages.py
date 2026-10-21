"""Workpackage definitions, signal mappings and WP evaluations.

Revision ID: 0009_workpackages
Revises: 0008_calibration_value_types
"""
import sqlalchemy as sa
from alembic import op


revision = "0009_workpackages"
down_revision = "0008_calibration_value_types"
branch_labels = None
depends_on = None


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "workpackages" not in tables:
        op.create_table(
            "workpackages",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("wp_code", sa.String(32), nullable=False, unique=True),
            sa.Column("title", sa.String(512), nullable=False),
            sa.Column("approved_revision_id", sa.String(32), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_workpackages_wp_code", "workpackages", ["wp_code"])
    if "workpackage_revisions" not in tables:
        op.create_table(
            "workpackage_revisions",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("workpackage_id", sa.String(32), sa.ForeignKey("workpackages.id"), nullable=False),
            sa.Column("number", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
            sa.Column("definition_json", sa.Text(), nullable=False),
            sa.Column("content_sha256", sa.String(64), nullable=False),
            sa.Column("review_notes", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("created_by", sa.String(32), nullable=False, server_default="user"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("workpackage_id", "number"),
        )
        op.create_index("ix_workpackage_revisions_workpackage_id", "workpackage_revisions", ["workpackage_id"])
    if "signal_mappings" not in tables:
        op.create_table(
            "signal_mappings",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("a2l_name", sa.String(256), nullable=False),
            sa.Column("mf4_channel", sa.String(256), nullable=False),
            sa.Column("match_layer", sa.String(16), nullable=False),
            sa.Column("match_status", sa.String(16), nullable=False, server_default="proposed"),
            sa.Column("status", sa.String(32), nullable=False, server_default="active"),
            sa.Column("supersedes_id", sa.String(32), nullable=True),
            sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_by", sa.String(32), nullable=False, server_default="system"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_signal_mappings_a2l_name", "signal_mappings", ["a2l_name"])
    if "wp_evaluations" not in tables:
        op.create_table(
            "wp_evaluations",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id"), nullable=False),
            sa.Column("workpackage_id", sa.String(32), sa.ForeignKey("workpackages.id"), nullable=False),
            sa.Column("revision_id", sa.String(32), sa.ForeignKey("workpackage_revisions.id"), nullable=False),
            sa.Column("dataset_id", sa.String(32), sa.ForeignKey("datasets.id"), nullable=False),
            sa.Column("a2l_sha256", sa.String(64), nullable=False, server_default=""),
            sa.Column("hex_sha256", sa.String(64), nullable=False, server_default=""),
            sa.Column("summary_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_wp_evaluations_workpackage_id", "wp_evaluations", ["workpackage_id"])
        op.create_index("ix_wp_evaluations_dataset_id", "wp_evaluations", ["dataset_id"])


def downgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    for name in ("wp_evaluations", "signal_mappings", "workpackage_revisions", "workpackages"):
        if name in tables:
            op.drop_table(name)
