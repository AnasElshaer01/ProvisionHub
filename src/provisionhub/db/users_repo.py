"""Users repository — the only file that knows ORM rows exist.

The SCIM route, the service, and the connectors all take/return the canonical
Pydantic `User`. Conversion ORM <-> Pydantic lives here.

Why a repo and not just SQLAlchemy calls inline in the route:
- one place to enforce id generation, `updated_at`, default emails shape
- swap to a different store without touching the route
- repository is what tests mock when they want to assert "did the route try
  to save what we expected" without spinning a DB
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.user import Email, User
from .orm import UserORM


class UserNotFoundError(LookupError):
    pass


class UsersRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    # ---- read ----------------------------------------------------------------

    async def get(self, user_id: str) -> User:
        row = await self._session.get(UserORM, user_id)
        if row is None:
            raise UserNotFoundError(user_id)
        return _to_pydantic(row)

    async def get_by_external_id(self, external_id: str) -> User | None:
        stmt = select(UserORM).where(UserORM.external_id == external_id)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_pydantic(row) if row else None

    async def get_by_user_name(self, user_name: str) -> User | None:
        stmt = select(UserORM).where(UserORM.user_name == user_name)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _to_pydantic(row) if row else None

    async def list_(self, *, user_name: str | None = None) -> list[User]:
        """Supports the one SCIM filter we honor: `userName eq "x"`."""
        stmt = select(UserORM)
        if user_name is not None:
            stmt = stmt.where(UserORM.user_name == user_name)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_pydantic(r) for r in rows]

    # ---- write ---------------------------------------------------------------

    async def create(self, user: User) -> User:
        """Insert. Generates id if not set. Caller commits."""
        row = UserORM(
            id=user.id or _new_id(),
            external_id=user.external_id,
            user_name=user.user_name,
            active=user.active,
            given_name=user.given_name,
            family_name=user.family_name,
            emails=[e.model_dump() for e in user.emails],
        )
        self._session.add(row)
        await self._session.flush()  # populate defaults (created_at/updated_at)
        return _to_pydantic(row)

    async def update(self, user_id: str, user: User) -> User:
        """Full replace (SCIM PUT). Caller commits."""
        row = await self._session.get(UserORM, user_id)
        if row is None:
            raise UserNotFoundError(user_id)
        row.external_id = user.external_id
        row.user_name = user.user_name
        row.active = user.active
        row.given_name = user.given_name
        row.family_name = user.family_name
        row.emails = [e.model_dump() for e in user.emails]
        await self._session.flush()
        return _to_pydantic(row)

    async def set_active(self, user_id: str, active: bool) -> User:
        """SCIM PATCH active=false is the deactivation we actually run."""
        row = await self._session.get(UserORM, user_id)
        if row is None:
            raise UserNotFoundError(user_id)
        row.active = active
        await self._session.flush()
        return _to_pydantic(row)

    async def delete(self, user_id: str) -> User:
        """SCIM DELETE → soft delete: active=false, audit row preserved.

        We deliberately do NOT remove the row. The README documents this as
        intentional: a re-`PUT` from Entra finds the user by external_id and
        flips active back on, and the audit history stays joinable.
        """
        return await self.set_active(user_id, False)


# ---------------------------------------------------------------------------
# ORM <-> Pydantic
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return str(uuid.uuid4())


def _to_pydantic(row: UserORM) -> User:
    return User(
        id=row.id,
        external_id=row.external_id,
        user_name=row.user_name,
        active=row.active,
        given_name=row.given_name,
        family_name=row.family_name,
        emails=[Email(**e) for e in (row.emails or [])],
    )
