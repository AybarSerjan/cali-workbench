"""Add editable MDF annotations.

Revision ID: 0003_dataset_annotations
Revises: 0002_calibration_commits
"""
from alembic import op

from backend.models import DatasetAnnotation

revision = "0003_dataset_annotations"
down_revision = "0002_calibration_commits"
branch_labels = None
depends_on = None


def upgrade():
    # Recreate cleanly: an earlier dev build created this table with a different
    # column set outside Alembic. Annotations are re-derivable from the files.
    DatasetAnnotation.__table__.drop(bind=op.get_bind(), checkfirst=True)
    DatasetAnnotation.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    DatasetAnnotation.__table__.drop(bind=op.get_bind(), checkfirst=True)
