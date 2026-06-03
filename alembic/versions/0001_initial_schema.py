"""initial schema: users, provisioning_events, connector_calls

Revision ID: 0001
Revises:
Create Date: 2026-06-04

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("user_name", sa.String(), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("given_name", sa.String(), nullable=True),
        sa.Column("family_name", sa.String(), nullable=True),
        sa.Column("emails", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_external_id", "users", ["external_id"])

    op.create_table(
        "provisioning_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("correlation_id", sa.String(), nullable=False),
        sa.Column("op", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_provisioning_events_correlation_id", "provisioning_events", ["correlation_id"])
    op.create_index("ix_provisioning_events_user_id", "provisioning_events", ["user_id"])

    op.create_table(
        "connector_calls",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("event_id", sa.String(), sa.ForeignKey("provisioning_events.id"), nullable=False),
        sa.Column("connector", sa.String(), nullable=False),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_connector_calls_event_id", "connector_calls", ["event_id"])
    op.create_index("ix_connector_calls_event_attempt", "connector_calls", ["event_id", "attempt"])


def downgrade() -> None:
    op.drop_index("ix_connector_calls_event_attempt", table_name="connector_calls")
    op.drop_index("ix_connector_calls_event_id", table_name="connector_calls")
    op.drop_table("connector_calls")

    op.drop_index("ix_provisioning_events_user_id", table_name="provisioning_events")
    op.drop_index("ix_provisioning_events_correlation_id", table_name="provisioning_events")
    op.drop_table("provisioning_events")

    op.drop_index("ix_users_external_id", table_name="users")
    op.drop_table("users")
