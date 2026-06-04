"""Remote-IDs repository — (user, connector) -> downstream remote_id.

Two methods. `get` is the lookup the service runs before update/deactivate.
`upsert` is what we call after a successful `create_user` returns a fresh
remote_id (and what's used for the rare case of re-provisioning the same
user against the same connector — the row is replaced, not duplicated).

Single-statement UPSERT is portable across SQLite (3.24+) and Postgres
via SQLAlchemy's dialect-specific helpers, but for v1 we do the
read-then-write dance: simple, explicit, no dialect import.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .orm import UserRemoteIdORM


class RemoteIdsRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, user_id: str, connector: str) -> str | None:
        stmt = select(UserRemoteIdORM).where(
            UserRemoteIdORM.user_id == user_id,
            UserRemoteIdORM.connector == connector,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return row.remote_id if row else None

    async def upsert(self, user_id: str, connector: str, remote_id: str) -> None:
        row = await self._session.get(UserRemoteIdORM, (user_id, connector))
        if row is None:
            self._session.add(UserRemoteIdORM(
                user_id=user_id, connector=connector, remote_id=remote_id,
            ))
        else:
            row.remote_id = remote_id
        await self._session.flush()
