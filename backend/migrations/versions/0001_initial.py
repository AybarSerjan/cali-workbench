"""Initial CALI catalog schema.

Revision ID: 0001_initial
Revises:
"""
from alembic import op

from backend.db import Base
from backend import models  # noqa: F401

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.create_all(bind=op.get_bind())
    op.execute("CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(article_id UNINDEXED, title, content)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS wiki_fts")
    Base.metadata.drop_all(bind=op.get_bind())

