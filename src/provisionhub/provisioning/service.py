"""ProvisioningService — the orchestrator.

Loads the canonical user, iterates the connector registry, calls each
connector's lifecycle method with retry, writes one connector_calls row
per HTTP attempt, persists remote_ids on successful creates, and
*continues iterating* when one connector fails. The route never names a
connector; this service never names one either.

Key invariants:
- Owns its own session (the route's session is closed by the time the
  service runs under the async-queue mode).
- Maps SCIM `patch` -> update_user. The connector ABC has 3 methods,
  not 4 (locked decision).
- `update` for a user with no remote_id yet falls through to `create_user`
  (self-healing: a Slack outage during initial create is recovered by the
  next PUT from Entra).
- Partial failure: one connector failing after retries does NOT stop the
  others. The connector_calls row with succeeded=false is the operator
  signal; PLAN.md §11 names this as the failure mode handled.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from ..connectors.base import Connector
from ..connectors.registry import ConnectorRegistry
from ..db.connector_calls_repo import ConnectorCallsRepository
from ..db.remote_ids_repo import RemoteIdsRepository
from ..db.users_repo import UsersRepository
from ..domain.user import User
from .job import ProvisioningJob
from . import retry as _retry  # late-bind DEFAULT_BACKOFFS_S so tests can monkeypatch

log = logging.getLogger(__name__)


class ProvisioningService:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        registry: ConnectorRegistry,
    ):
        self._session_factory = session_factory
        self._registry = registry

    async def run(self, job: ProvisioningJob) -> None:
        async with self._session_factory() as session:
            users = UsersRepository(session)
            calls = ConnectorCallsRepository(session)
            remotes = RemoteIdsRepository(session)

            user = await users.get(job.user_id)

            for connector in self._registry.enabled():
                try:
                    await self._dispatch(connector, job, user, remotes, calls)
                except Exception:
                    # Logged + audited (succeeded=false rows already written).
                    # We DO NOT stop iterating — partial provisioning is by design.
                    log.exception(
                        "connector %s exhausted retries event_id=%s",
                        connector.name, job.event_id,
                    )

            await session.commit()

    # ------------------------------------------------------------------ dispatch
    async def _dispatch(
        self,
        connector: Connector,
        job: ProvisioningJob,
        user: User,
        remotes: RemoteIdsRepository,
        calls: ConnectorCallsRepository,
    ) -> None:
        """Pick the right connector method for `job.op`, run with retry,
        write one connector_calls row per attempt, persist remote_id on
        successful create.
        """
        remote_id = await remotes.get(user.id, connector.name)
        op = job.op

        # Decide the effective op + the awaitable to retry.
        if op == "create" or ((op in ("update", "patch")) and remote_id is None):
            effective_op = "create"
            endpoint = f"{connector.name}.create_user"
            request_body = {"user": user.model_dump(mode="json")}

            async def call():
                return await connector.create_user(user)

        elif op in ("update", "patch"):
            effective_op = "update"
            endpoint = f"{connector.name}.update_user"
            request_body = {"remote_id": remote_id, "user": user.model_dump(mode="json")}

            async def call():
                await connector.update_user(remote_id, user)
                return None

        elif op == "delete":
            if remote_id is None:
                # Nothing to deactivate downstream. Skip silently — the user
                # never reached this connector, there's nothing to undo.
                return
            effective_op = "deactivate"
            endpoint = f"{connector.name}.deactivate_user"
            request_body = {"remote_id": remote_id}

            async def call():
                await connector.deactivate_user(remote_id)
                return None
        else:  # pragma: no cover — Op is a Literal, mypy enforces this
            raise ValueError(f"unknown op: {op}")

        outcomes = await _retry.run_with_retry(call, backoffs_s=_retry.DEFAULT_BACKOFFS_S)

        # Write one connector_calls row per attempt, in order.
        for outcome in outcomes:
            await calls.record_attempt(
                event_id=job.event_id,
                connector=connector.name,
                endpoint=endpoint,
                request=request_body,
                attempt=outcome.attempt,
                succeeded=outcome.succeeded,
                response_status=outcome.response_status,
                response={"remote_id": outcome.result} if outcome.succeeded and effective_op == "create" else None,
                latency_ms=outcome.latency_ms,
                error=repr(outcome.error) if outcome.error else None,
            )

        # Persist remote_id when a create succeeded.
        last = outcomes[-1]
        if last.succeeded and effective_op == "create":
            await remotes.upsert(user.id, connector.name, last.result)

        # Re-raise so .run()'s outer try/except logs at exception level;
        # the audit row already records the failure.
        if not last.succeeded:
            assert last.error is not None
            raise last.error
