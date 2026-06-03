"""Connector-calls repository — outbound HTTP audit, insert-only.

One row per HTTP attempt against a downstream connector. Retries write
multiple rows for the same event_id, distinguished by `attempt`.

There is intentionally only one method here. If you find yourself adding
an `update_call` or `delete_call`, you're modeling something wrong
(CLAUDE.md: audit tables are insert-only).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from .orm import ConnectorCallORM


class ConnectorCallsRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def record_attempt(
        self,
        *,
        event_id: str,
        connector: str,
        endpoint: str,
        request: dict,
        attempt: int,
        succeeded: bool,
        response_status: int | None = None,
        response: dict | None = None,
        latency_ms: int | None = None,
        error: str | None = None,
    ) -> str:
        """Insert one audit row. Returns the new call id."""
        call_id = str(uuid.uuid4())
        self._session.add(
            ConnectorCallORM(
                id=call_id,
                event_id=event_id,
                connector=connector,
                endpoint=endpoint,
                request=request,
                response_status=response_status,
                response=response,
                latency_ms=latency_ms,
                attempt=attempt,
                succeeded=succeeded,
                error=error,
            )
        )
        await self._session.flush()
        return call_id
