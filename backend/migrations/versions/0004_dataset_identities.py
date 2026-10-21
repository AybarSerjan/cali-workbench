"""Add stable dataset serials.

Revision ID: 0004_dataset_identities
Revises: 0003_dataset_annotations
"""
from alembic import op

from backend.models import DatasetIdentity

revision = "0004_dataset_identities"
down_revision = "0003_dataset_annotations"
branch_labels = None
depends_on = None


def upgrade():
    DatasetIdentity.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    DatasetIdentity.__table__.drop(bind=op.get_bind(), checkfirst=True)
