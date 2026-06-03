"""Events repository — inbound audit writes.

`provisioning_events` is insert-only by convention (CLAUDE.md). This repo
exposes one method per write shape so nobody is tempted to update or delete
rows here.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from .orm import ProvisioningEventORM
from ..provisioning.job import Op


class EventsRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def record_inbound(
        self,
        *,
        correlation_id: str,
        op: Op,
        user_id: str | None,
        payload: dict,
    ) -> str:
        """Insert one inbound audit row. Returns the new event_id."""
        event_id = str(uuid.uuid4())
        row = ProvisioningEventORM(
            id=event_id,
            correlation_id=correlation_id,
            op=op,
            user_id=user_id,
            payload=payload,
        )
        self._session.add(row)
        await self._session.flush()
        return event_id
