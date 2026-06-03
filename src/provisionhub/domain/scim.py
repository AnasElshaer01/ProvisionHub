"""SCIM 2.0 Pydantic models + canonicalize().

SCIM payloads are nested and multi-valued (emails is a list, name is an
object). We accept them as-is, then flatten to the canonical User.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .user import Email, User


class SCIMName(BaseModel):
    givenName: str | None = None
    familyName: str | None = None


class SCIMEmail(BaseModel):
    value: str
    primary: bool = False
    type: str | None = None


class SCIMUser(BaseModel):
    """SCIM 2.0 User resource. What Entra POSTs to /scim/v2/Users."""

    model_config = ConfigDict(extra="allow")  # tolerate Entra extras

    schemas: list[str] = Field(default_factory=lambda: ["urn:ietf:params:scim:schemas:core:2.0:User"])
    id: str | None = None
    externalId: str | None = None
    userName: str
    active: bool = True
    name: SCIMName | None = None
    emails: list[SCIMEmail] = Field(default_factory=list)


class SCIMPatchOperation(BaseModel):
    op: Literal["add", "replace", "remove", "Add", "Replace", "Remove"]
    path: str | None = None
    value: Any = None


class SCIMPatchOp(BaseModel):
    """SCIM PATCH envelope: { schemas, Operations: [...] }."""

    schemas: list[str] = Field(
        default_factory=lambda: ["urn:ietf:params:scim:api:messages:2.0:PatchOp"]
    )
    Operations: list[SCIMPatchOperation]


def canonicalize(scim: SCIMUser) -> User:
    """SCIM User → canonical User. Preserves all emails as-sent."""
    return User(
        external_id=scim.externalId,
        user_name=scim.userName,
        active=scim.active,
        given_name=scim.name.givenName if scim.name else None,
        family_name=scim.name.familyName if scim.name else None,
        emails=[Email(value=e.value, primary=e.primary) for e in scim.emails],
    )
