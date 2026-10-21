"""Add INCA metadata and scan status.

Revision ID: 0006_inca_metadata
Revises: 0005_source_link
"""
import sqlalchemy as sa
from alembic import op


revision = "0006_inca_metadata"
down_revision = "0005_source_link"
branch_labels = None
depends_on = None


def upgrade():
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("dataset_annotations")}
    additions = (
        sa.Column("program_description", sa.Text(), nullable=False, server_default=""),
        sa.Column("working_version", sa.Text(), nullable=False, server_default=""),
        sa.Column("reference_version", sa.Text(), nullable=False, server_default=""),
        sa.Column("metadata_parser_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata_scan_error", sa.Text(), nullable=False, server_default=""),
    )
    for column in additions:
        if column.name not in columns:
            op.add_column("dataset_annotations", column)


def downgrade():
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("dataset_annotations")}
    for name in ("metadata_scan_error", "metadata_parser_version", "reference_version", "working_version", "program_description"):
        if name in columns:
            op.drop_column("dataset_annotations", name)
