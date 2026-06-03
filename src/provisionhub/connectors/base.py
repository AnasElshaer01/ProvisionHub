"""Connector ABC — the interface every downstream app implements.

Three methods only. The service maps SCIM `patch` to `update_user` internally
so connector authors don't have to think about PATCH semantics.

Idempotency rule (CLAUDE.md): every connector's `create_user` MUST look up
by externalId before creating. The service has no way to enforce this from
the outside — it's an honor-system invariant documented in tests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from ..domain.user import User


class Connector(ABC):
    name: ClassVar[str]  # "slack" | "jira" | ...

    @abstractmethod
    async def create_user(self, user: User) -> str:
        """Provision the user. Returns the connector-side remote_id."""

    @abstractmethod
    async def update_user(self, remote_id: str, user: User) -> None:
        """Replace the downstream user with `user`'s attributes."""

    @abstractmethod
    async def deactivate_user(self, remote_id: str) -> None:
        """Disable the downstream user. No hard-delete by design."""
