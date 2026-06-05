"""ProvisioningService — the orchestrator.

Loads the canonical user, fans out across the connector registry **in
parallel**, runs each connector with its own retries, writes one
`connector_calls` row per HTTP attempt, persists remote_ids on successful
creates, and *does not* let one connector failing stop the others.

The route never names a connector; this service never names one either.

Key invariants:
- Owns its own session (the route's session is closed by the time the
  service runs in the background worker).
- Maps SCIM `patch` -> update_user. The connector ABC has 3 methods,
  not 4 (locked decision).
- `update` for a user with no remote_id yet falls through to `create_user`
  (self-healing: a Slack outage during initial create is recovered by the
  next PUT from Entra).
- Partial failure: connectors run concurrently via `asyncio.gather` with
  `return_exceptions=True`. One connector exhausting retries does NOT stop
  the others. The connector_calls row with succeeded=false is the operator
  signal; PLAN.md §11 names this as the failure mode handled.
- Audit writes are serialized by an asyncio.Lock because SQLAlchemy async
  sessions are single-consumer — the HTTP fan-out is parallel, the DB
  writes that record each attempt are not.

Per-job reporting:
- For every connector that exhausts retries: one structured
  `connector.exhausted` log line at ERROR.
- For every job that finishes: one `job.complete` log line at INFO
  summarizing per-connector outcomes (`success | partial | failure`).
"""

from __future__ import annotations

import asyncio
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

            connectors = list(self._registry.enabled())

            # Serialize audit writes across the gather — the slow HTTP work
            # runs concurrently, but the session can only have one writer
            # at a time. (Async session is single-consumer.)
            db_lock = asyncio.Lock()

            # Run all connectors concurrently. return_exceptions=True so one
            # connector failing doesn't cancel the others — Pattern B partial
            # failure preserved, just faster.
            results = await asyncio.gather(
                *[
                    self._dispatch(c, job, user, remotes, calls, db_lock)
                    for c in connectors
                ],
                return_exceptions=True,
            )

            await session.commit()

            # ---- per-connector failure log + job summary -----------------
            outcomes: dict[str, str] = {}
            for c, r in zip(connectors, results):
                if isinstance(r, BaseException):
                    outcomes[c.name] = "failed"
                    log.error(
                        "connector.exhausted connector=%s event_id=%s error=%r",
                        c.name, job.event_id, r,
                    )
                else:
                    outcomes[c.name] = "ok"

            if not outcomes:
                outcome = "success"  # no connectors registered = nothing to fail
            elif all(v == "failed" for v in outcomes.values()):
                outcome = "failure"
            elif any(v == "failed" for v in outcomes.values()):
                outcome = "partial"
            else:
                outcome = "success"

            log.info(
                "job.complete event_id=%s correlation_id=%s op=%s outcome=%s connectors=%s",
                job.event_id, job.correlation_id, job.op, outcome,
                ",".join(f"{n}:{v}" for n, v in outcomes.items()) or "(none)",
            )

    # ------------------------------------------------------------------ dispatch
    async def _dispatch(
        self,
        connector: Connector,
        job: ProvisioningJob,
        user: User,
        remotes: RemoteIdsRepository,
        calls: ConnectorCallsRepository,
        db_lock: asyncio.Lock,
    ) -> None:
        """Pick the right connector method for `job.op`, run with retry,
        write one connector_calls row per attempt, persist remote_id on
        successful create.

        DB writes are taken under `db_lock` so concurrent dispatches don't
        race on the shared session. The HTTP retry work is OUTSIDE the lock
        — that's where the parallelism we want lives.
        """
        async with db_lock:
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

        # The slow part — HTTP fan-out + retries. NOT under db_lock; this
        # is where Slack and Jira make progress in parallel.
        outcomes = await _retry.run_with_retry(call, backoffs_s=_retry.DEFAULT_BACKOFFS_S)

        # Audit writes are serialized so the shared session stays sane.
        async with db_lock:
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

            last = outcomes[-1]
            if last.succeeded and effective_op == "create":
                await remotes.upsert(user.id, connector.name, last.result)

        # Re-raise so gather captures it as a per-connector failure marker.
        # The audit row already records the failure; this is what the
        # outer summary loop reads to produce the job.complete line.
        if not last.succeeded:
            assert last.error is not None
            raise last.error
