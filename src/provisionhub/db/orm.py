"""SQLAlchemy ORM — 3 tables.

- users               : the canonical user row (one row per provisioned user)
- provisioning_events : inbound SCIM audit, insert-only
- connector_calls     : one row per outbound HTTP attempt, insert-only

`JSON` is SQLAlchemy's portable type, not Postgres-specific JSONB. SQLite
stores it as TEXT; Postgres would store it as JSON/JSONB. Same Python API
either way, which is the whole point.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class UserORM(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    external_id: Mapped[str | None] = mapped_column(String, index=True)
    user_name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    given_name: Mapped[str | None] = mapped_column(String)
    family_name: Mapped[str | None] = mapped_column(String)
    emails: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class ProvisioningEventORM(Base):
    """Inbound audit — one row per SCIM request we accept. Insert-only."""

    __tablename__ = "provisioning_events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    op: Mapped[str] = mapped_column(String, nullable=False)  # create|update|patch|delete
    user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class ConnectorCallORM(Base):
    """Outbound audit — one row per HTTP attempt against a connector. Insert-only.

    Retries write multiple rows for the same `event_id`, distinguished by `attempt`.
    """

    __tablename__ = "connector_calls"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(String, ForeignKey("provisioning_events.id"), index=True, nullable=False)
    connector: Mapped[str] = mapped_column(String, nullable=False)  # slack|jira|...
    endpoint: Mapped[str] = mapped_column(String, nullable=False)
    request: Mapped[dict] = mapped_column(JSON, nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response: Mapped[dict | None] = mapped_column(JSON)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


# Composite index for the admin endpoint: "all calls for user X" goes through
# event_id, but the typical query joins events.user_id -> connector_calls.event_id.
# The single-column indexes above already cover that path.
Index("ix_connector_calls_event_attempt", ConnectorCallORM.event_id, ConnectorCallORM.attempt)
