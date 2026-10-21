"""Add parameter-level calibration traceability.

Revision ID: 0007_calibration_traceability
Revises: 0006_message_meta, 0006_inca_metadata
"""
import sqlalchemy as sa
from alembic import op


revision = "0007_calibration_traceability"
down_revision = ("0006_message_meta", "0006_inca_metadata")
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "calibration_sessions" not in tables:
        op.create_table(
            "calibration_sessions",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("a2l_file_id", sa.String(32), sa.ForeignKey("source_files.id"), nullable=False),
            sa.Column("hex_file_ids", sa.Text(), nullable=False),
            sa.Column("session_summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_by", sa.String(32), nullable=False, server_default="user"),
        )
        op.create_index("ix_calibration_sessions_a2l_file_id", "calibration_sessions", ["a2l_file_id"])
    if "parameter_changes" not in tables:
        op.create_table(
            "parameter_changes",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("session_id", sa.String(32), sa.ForeignKey("calibration_sessions.id"), nullable=False),
            sa.Column("parameter_name", sa.String(256), nullable=False),
            sa.Column("module", sa.String(256), nullable=False),
            sa.Column("data_type", sa.String(64), nullable=False),
            sa.Column("unit", sa.String(64), nullable=False, server_default=""),
            sa.Column("range", sa.String(128), nullable=False, server_default=""),
            sa.Column("a2l_description", sa.Text(), nullable=False, server_default=""),
            sa.Column("old_value", sa.Text(), nullable=False),
            sa.Column("new_value", sa.Text(), nullable=False),
            sa.Column("change_summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_parameter_changes_session_id", "parameter_changes", ["session_id"])
        op.create_index("ix_parameter_changes_parameter_name", "parameter_changes", ["parameter_name"])
        op.create_index("ix_parameter_changes_module", "parameter_changes", ["module"])


def downgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "parameter_changes" in tables:
        op.drop_table("parameter_changes")
    if "calibration_sessions" in tables:
        op.drop_table("calibration_sessions")
