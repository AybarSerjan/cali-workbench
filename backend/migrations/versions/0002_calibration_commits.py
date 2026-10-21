"""Add calibration_commits table.

Revision ID: 0002_calibration_commits
Revises: 0001_initial
"""
from alembic import op

from backend.models import CalibrationCommit

revision = "0002_calibration_commits"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    CalibrationCommit.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    CalibrationCommit.__table__.drop(bind=op.get_bind(), checkfirst=True)
