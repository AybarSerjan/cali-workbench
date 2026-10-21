"""Add source_url to source_files for external link sources.

Revision ID: 0005_source_link
Revises: 0004_dataset_identities
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_source_link"
down_revision = "0004_dataset_identities"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("source_files", sa.Column("source_url", sa.String(2048), nullable=False, server_default=""))


def downgrade():
    op.drop_column("source_files", "source_url")
