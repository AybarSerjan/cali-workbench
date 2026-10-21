"""Add char_type and detail_json to parameter_changes for non-scalar diffs.

Revision ID: 0008_calibration_value_types
Revises: 0007_calibration_traceability
"""
import sqlalchemy as sa
from alembic import op


revision = "0008_calibration_value_types"
down_revision = "0007_calibration_traceability"
branch_labels = None
depends_on = None


def upgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("parameter_changes")}
    if "char_type" not in columns:
        op.add_column(
            "parameter_changes",
            sa.Column("char_type", sa.String(16), nullable=False, server_default="VALUE"),
        )
    if "detail_json" not in columns:
        op.add_column(
            "parameter_changes",
            sa.Column("detail_json", sa.Text(), nullable=False, server_default=""),
        )


def downgrade():
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("parameter_changes")}
    if "detail_json" in columns:
        op.drop_column("parameter_changes", "detail_json")
    if "char_type" in columns:
        op.drop_column("parameter_changes", "char_type")
