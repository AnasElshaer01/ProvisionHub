"""ProvisioningService — three tests, three claims.

1. A connector the service has never heard of is handled identically.
   This is THE extensibility proof CLAUDE.md insists on: not a doc claim,
   a test that fails if anyone adds an `if connector.name == ...` branch.

2. Retry semantics: 3 attempts on retriable failure, one audit row per
   attempt, last one succeeds when the underlying call recovers.

3. Partial failure: when one connector exhausts retries, the others
   still run, the successful ones still persist their remote_id, and
   service.run() returns normally. PLAN.md §11 — locked by test.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from provisionhub.connectors.base import Connector
from provisionhub.connectors.registry import ConnectorRegistry
from provisionhub.db.connector_calls_repo import ConnectorCallsRepository  # noqa: F401
from provisionhub.db.events_repo import EventsRepository
from provisionhub.db.orm import ConnectorCallORM, UserRemoteIdORM
from provisionhub.db.remote_ids_repo import RemoteIdsRepository  # noqa: F401
from provisionhub.db.users_repo import UsersRepository
from provisionhub.domain.user import Email, User
from provisionhub.provisioning.job import ProvisioningJob
from provisionhub.provisioning.retry import DEFAULT_BACKOFFS_S  # noqa: F401
from provisionhub.provisioning.service import ProvisioningService

# Use trivial backoffs in the service-level tests so retries are instant.
# We patch the module-level constant via monkeypatch in the fixture below.


# ---------------------------------------------------------------------------
# Fake connectors. Each is one shape we want to assert against.
# ---------------------------------------------------------------------------

class RecordingConnector(Connector):
    """Records what it's called with. Always succeeds."""
    name = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def create_user(self, user: User) -> str:
        self.calls.append(("create", user.user_name))
        return f"remote-{user.user_name}"

    async def update_user(self, remote_id: str, user: User) -> None:
        self.calls.append(("update", remote_id))

    async def deactivate_user(self, remote_id: str) -> None:
        self.calls.append(("deactivate", remote_id))


class FlakyConnector(Connector):
    """Fails N times with 503 then succeeds. For retry tests."""
    name = "flaky"

    def __init__(self, fail_times: int = 2) -> None:
        self.fail_times = fail_times
        self.attempts = 0

    async def create_user(self, user: User) -> str:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise httpx.HTTPStatusError(
                "503",
                request=httpx.Request("POST", "http://flaky"),
                response=httpx.Response(503),
            )
        return f"remote-flaky-{user.user_name}"

    async def update_user(self, remote_id: str, user: User) -> None: ...
    async def deactivate_user(self, remote_id: str) -> None: ...


class AlwaysFailingConnector(Connector):
    """Always 500s. For partial-failure tests."""
    name = "broken"

    async def create_user(self, user: User) -> str:
        raise httpx.HTTPStatusError(
            "500",
            request=httpx.Request("POST", "http://broken"),
            response=httpx.Response(500),
        )

    async def update_user(self, remote_id: str, user: User) -> None: ...
    async def deactivate_user(self, remote_id: str) -> None: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """Replace 1s/4s/16s with 0/0/0 for service-level tests."""
    monkeypatch.setattr(
        "provisionhub.provisioning.retry.DEFAULT_BACKOFFS_S",
        (0.0, 0.0, 0.0),
    )


async def _seed_user_and_event(session_factory) -> ProvisioningJob:
    """Insert a user + provisioning_event so the service has something to act on."""
    async with session_factory() as s:
        users = UsersRepository(s)
        events = EventsRepository(s)
        user = await users.create(User(
            user_name="alice@x.com",
            external_id="ext-alice",
            emails=[Email(value="alice@x.com", primary=True)],
        ))
        event_id = await events.record_inbound(
            correlation_id="corr-1",
            op="create",
            user_id=user.id,
            payload={"userName": "alice@x.com"},
        )
        await s.commit()
        return ProvisioningJob(
            event_id=event_id,
            correlation_id="corr-1",
            user_id=user.id,
            op="create",
        )


# ===========================================================================
# 1. Extensibility proof
# ===========================================================================

@pytest.mark.asyncio
async def test_service_handles_unknown_connector_identically(session_factory):
    """Register a connector the service has never seen. It is handled."""
    job = await _seed_user_and_event(session_factory)

    fake = RecordingConnector()
    registry = ConnectorRegistry()
    registry.register(fake)
    service = ProvisioningService(session_factory=session_factory, registry=registry)

    await service.run(job)

    # connector.create_user was invoked with the canonical user
    assert fake.calls == [("create", "alice@x.com")]

    # remote_id persisted via the repo path
    async with session_factory() as s:
        remote = (await s.execute(
            select(UserRemoteIdORM).where(UserRemoteIdORM.connector == "recording")
        )).scalar_one()
        assert remote.remote_id == "remote-alice@x.com"

        # one connector_calls row, succeeded
        calls = (await s.execute(select(ConnectorCallORM))).scalars().all()
        assert len(calls) == 1
        assert calls[0].connector == "recording"
        assert calls[0].succeeded is True
        assert calls[0].attempt == 1
        assert calls[0].event_id == job.event_id


# ===========================================================================
# 2. Retry semantics
# ===========================================================================

@pytest.mark.asyncio
async def test_service_writes_one_call_row_per_retry_attempt(session_factory):
    """503 -> 503 -> success: three audit rows, attempts 1,2,3, last succeeded."""
    job = await _seed_user_and_event(session_factory)

    flaky = FlakyConnector(fail_times=2)
    registry = ConnectorRegistry()
    registry.register(flaky)
    service = ProvisioningService(session_factory=session_factory, registry=registry)

    await service.run(job)

    async with session_factory() as s:
        calls = (await s.execute(
            select(ConnectorCallORM).order_by(ConnectorCallORM.attempt)
        )).scalars().all()
        assert [(c.attempt, c.succeeded, c.response_status) for c in calls] == [
            (1, False, 503),
            (2, False, 503),
            (3, True, None),
        ]

        # remote_id still got persisted on the successful attempt
        remote = (await s.execute(
            select(UserRemoteIdORM).where(UserRemoteIdORM.connector == "flaky")
        )).scalar_one()
        assert remote.remote_id == "remote-flaky-alice@x.com"


# ===========================================================================
# 3. Partial failure
# ===========================================================================

@pytest.mark.asyncio
async def test_partial_failure_other_connectors_still_run(session_factory):
    """broken always 500s; recording succeeds. service.run() returns cleanly,
    recording's remote_id is persisted, broken has 3 failed audit rows."""
    job = await _seed_user_and_event(session_factory)

    broken = AlwaysFailingConnector()
    recording = RecordingConnector()
    registry = ConnectorRegistry()
    registry.register(broken)     # registered first — failure must not block recording
    registry.register(recording)
    service = ProvisioningService(session_factory=session_factory, registry=registry)

    await service.run(job)   # must not raise

    async with session_factory() as s:
        # recording succeeded -> remote_id row present
        recording_remote = (await s.execute(
            select(UserRemoteIdORM).where(UserRemoteIdORM.connector == "recording")
        )).scalar_one()
        assert recording_remote.remote_id == "remote-alice@x.com"

        # broken has no remote_id row
        broken_remote = (await s.execute(
            select(UserRemoteIdORM).where(UserRemoteIdORM.connector == "broken")
        )).scalar_one_or_none()
        assert broken_remote is None

        # broken contributed 3 failed audit rows; recording contributed 1 success
        broken_calls = (await s.execute(
            select(ConnectorCallORM).where(ConnectorCallORM.connector == "broken")
            .order_by(ConnectorCallORM.attempt)
        )).scalars().all()
        assert [(c.attempt, c.succeeded) for c in broken_calls] == [(1, False), (2, False), (3, False)]

        recording_calls = (await s.execute(
            select(ConnectorCallORM).where(ConnectorCallORM.connector == "recording")
        )).scalars().all()
        assert len(recording_calls) == 1
        assert recording_calls[0].succeeded is True


# ===========================================================================
# 4. Concurrency — Slack and Jira run AT THE SAME TIME
# ===========================================================================

class SlowConnector(Connector):
    """Sleeps `delay_s` on create_user, then succeeds. For timing tests."""

    def __init__(self, name: str, delay_s: float) -> None:
        # Connector ABC uses `name` as a class attr; assign per-instance.
        self.name = name
        self.delay_s = delay_s

    async def create_user(self, user: User) -> str:
        import asyncio
        await asyncio.sleep(self.delay_s)
        return f"remote-{self.name}-{user.user_name}"

    async def update_user(self, remote_id: str, user: User) -> None: ...
    async def deactivate_user(self, remote_id: str) -> None: ...


@pytest.mark.asyncio
async def test_connectors_run_concurrently(session_factory):
    """Two connectors that each sleep 100ms must finish in <180ms wall-clock.
    If anyone reverts the gather() back to a sequential for-loop, this fails
    (sequential would be ~200ms+)."""
    import asyncio
    import time as _time

    job = await _seed_user_and_event(session_factory)

    slack = SlowConnector(name="slack-fake", delay_s=0.1)
    jira = SlowConnector(name="jira-fake", delay_s=0.1)
    registry = ConnectorRegistry()
    registry.register(slack)
    registry.register(jira)
    service = ProvisioningService(session_factory=session_factory, registry=registry)

    start = _time.perf_counter()
    await service.run(job)
    elapsed = _time.perf_counter() - start

    assert elapsed < 0.18, f"expected <180ms (parallel), got {elapsed*1000:.0f}ms"

    # Both succeeded, both have remote_ids.
    async with session_factory() as s:
        remotes = (await s.execute(select(UserRemoteIdORM))).scalars().all()
        names = sorted(r.connector for r in remotes)
        assert names == ["jira-fake", "slack-fake"]
