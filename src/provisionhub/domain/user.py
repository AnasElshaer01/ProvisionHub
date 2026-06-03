"""Canonical internal User.

This is the shape used everywhere downstream of SCIM parsing: queue, service,
connectors, ORM. Connector-specific fields (Jira accountId, Slack profile)
stay in connector modules — never here.

`emails` is the one list we keep: SCIM is multi-valued here and dropping
secondaries on the floor would be lossy.
"""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class Email(BaseModel):
    value: EmailStr  # validated as a real email address
    primary: bool = False


class User(BaseModel):
    id: str | None = None
    external_id: str | None = None
    user_name: str
    active: bool = True
    given_name: str | None = None
    family_name: str | None = None
    emails: list[Email] = Field(default_factory=list)
