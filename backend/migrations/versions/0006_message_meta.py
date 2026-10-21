"""Add meta_json to messages for task/agent mode payloads.

Revision ID: 0006_message_meta
Revises: 0005_source_link
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_message_meta"
down_revision = "0005_source_link"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("messages", sa.Column("meta_json", sa.Text(), nullable=False, server_default="{}"))


def downgrade():
    op.drop_column("messages", "meta_json")
