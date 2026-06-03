"""user_remote_ids: per-(user, connector) -> downstream remote_id

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-04

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_remote_ids",
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("connector", sa.String(), primary_key=True),
        sa.Column("remote_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_remote_ids")
