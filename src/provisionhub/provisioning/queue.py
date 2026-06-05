"""JobQueue — the architectural seam between "accept" and "process".

The SCIM route always calls `queue.submit(job)`. It never calls the service
directly. The route returns 201 to Entra in milliseconds; the work — calling
out to Slack, Jira, retrying flaky downstreams — happens in a background
worker on the same event loop.

Shape:

    submit(job)  ──► asyncio.Queue ──► _worker() ──► service.run(job)
       ▲                                  ▲
       │                                  │
    fast: returns                    long: HTTP fan-out + retries
    immediately                      (parallel across connectors)

Lifecycle:

  - `start()` spawns the background worker. Called once from FastAPI
    lifespan on startup.
  - `stop()` puts a poison-pill sentinel on the queue and awaits the
    worker draining. Called once from FastAPI lifespan on shutdown.

Single consumer task. One job is dispatched at a time; per-job parallelism
(across connectors) lives inside `ProvisioningService.run` via
`asyncio.gather`. If you need across-job parallelism later, spawn N workers
in `start()` — `asyncio.Queue` is N-consumer safe and `service.run` opens
its own session per job.

Durability: the queue is in-process. If the process dies with jobs queued,
those jobs are lost. The `provisioning_events` row was committed by the
route before submit, so the inbound audit is durable; only the outbound
attempts are lost. Closing this gap is what a real broker (Redis/SQS) is
for — and the seam is `submit(job)` itself, so swapping the broker in
later means changing the body of this file and nothing else.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .job import ProvisioningJob

if TYPE_CHECKING:
    from .service import ProvisioningService

log = logging.getLogger(__name__)


# Sentinel placed on the queue by stop() to tell the worker to exit cleanly.
_SHUTDOWN: object = object()


class JobQueue:
    def __init__(self, service: "ProvisioningService"):
        self._service = service
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ submit
    async def submit(self, job: ProvisioningJob) -> None:
        """Enqueue a job for the background worker. Returns immediately."""
        log.info(
            "queue.submit event_id=%s correlation_id=%s op=%s user_id=%s",
            job.event_id, job.correlation_id, job.op, job.user_id,
        )
        await self._queue.put(job)

    # --------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        """Spawn the background worker. Called from FastAPI lifespan startup."""
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker(), name="job-queue-worker")
            log.info("queue worker started")

    async def stop(self) -> None:
        """Drain in-flight jobs and stop the worker. Called from lifespan shutdown."""
        if self._worker_task is None:
            return
        await self._queue.put(_SHUTDOWN)
        await self._worker_task
        self._worker_task = None
        log.info("queue worker stopped")

    # ------------------------------------------------------------------ worker
    async def _worker(self) -> None:
        """Pull jobs off the queue and run them, one at a time.

        One bad job must not kill the loop. The connector_calls row written
        by the service already records the failure; we log at exception
        level and move on so the next job still runs.
        """
        while True:
            item = await self._queue.get()
            try:
                if item is _SHUTDOWN:
                    return
                try:
                    await self._service.run(item)
                except Exception:
                    log.exception("job failed event_id=%s", item.event_id)
            finally:
                self._queue.task_done()
