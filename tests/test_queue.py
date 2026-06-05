"""Queue seam — pinned from both sides.

Top half: unit tests that JobQueue.submit eventually invokes service.run
through the background worker, and that one bad job doesn't kill the loop.
Bottom half: integration test that the SCIM route calls queue.submit,
not service.run directly.

If anyone "simplifies" the route by skipping the queue, the bottom test
fails. If anyone replaces submit with a no-op, the top tests fail.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from provisionhub.api import scim_users
from provisionhub.provisioning.job import ProvisioningJob
from provisionhub.provisioning.queue import JobQueue


@pytest.mark.asyncio
async def test_submit_routes_through_worker_to_service_run_once():
    """submit -> queue -> worker -> service.run. Exactly once per submission."""
    fake_service = AsyncMock()
    queue = JobQueue(service=fake_service)
    job = ProvisioningJob(event_id="e1", correlation_id="c1", user_id="u1", op="create")

    await queue.start()
    try:
        await queue.submit(job)
        # Wait for the worker to drain the queue before asserting.
        await queue._queue.join()
    finally:
        await queue.stop()

    fake_service.run.assert_awaited_once_with(job)


@pytest.mark.asyncio
async def test_worker_survives_a_failing_job():
    """A connector blowing up must not kill the worker. The next job still runs."""
    fake_service = AsyncMock()
    fake_service.run.side_effect = [RuntimeError("downstream on fire"), None]
    queue = JobQueue(service=fake_service)
    job1 = ProvisioningJob(event_id="e1", correlation_id="c1", user_id="u1", op="create")
    job2 = ProvisioningJob(event_id="e2", correlation_id="c2", user_id="u2", op="create")

    await queue.start()
    try:
        await queue.submit(job1)
        await queue.submit(job2)
        await queue._queue.join()
    finally:
        await queue.stop()

    assert fake_service.run.await_count == 2


@pytest.mark.asyncio
async def test_route_calls_queue_submit_not_service_directly(client):
    """The SCIM POST route MUST go through queue.submit.
    If a future refactor wires service.run directly into the route,
    this test catches it."""
    fake_queue = AsyncMock()
    # client fixture's app already has a real queue wired; override it.
    client._transport.app.dependency_overrides[scim_users.get_queue] = lambda: fake_queue

    response = await client.post(
        "/scim/v2/Users",
        headers={"Authorization": "Bearer dev-token"},
        json={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "externalId": "ext-1",
            "userName": "queue-seam@x.com",
        },
    )

    assert response.status_code == 201
    fake_queue.submit.assert_awaited_once()
    job = fake_queue.submit.await_args.args[0]
    assert isinstance(job, ProvisioningJob)
    assert job.op == "create"
    assert job.user_id  # populated by repo.create -> uuid4
