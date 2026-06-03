"""JobQueue — the architectural seam.

The SCIM route always calls `queue.submit(job)`. It never calls the service
directly. The body of `submit` is the only thing that changes when we go
from inline -> in-process async queue -> Redis broker.

This file ships TWO modes:

  1. INLINE (active, default)
     submit() awaits service.run(job) right there in the request handler.
     Simple, fast to demo, no background tasks. The Day 1 default.

  2. IN-PROCESS ASYNC QUEUE (commented out)
     submit() puts the job on an asyncio.Queue and returns immediately.
     A background worker task pulls jobs and runs them. The route is no
     longer waiting on connector latency. Uncomment the marked block and
     wire `await queue.start()` / `await queue.stop()` in main.py lifespan.

  3. (Future) REAL BROKER
     submit() would push onto Redis/SQS; a separate worker process pulls
     and calls service.run. Same `submit(job)` signature — route doesn't know.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .job import ProvisioningJob

if TYPE_CHECKING:
    from .service import ProvisioningService

log = logging.getLogger(__name__)


class JobQueue:
    def __init__(self, service: "ProvisioningService | None" = None):
        self._service = service

        # ---- async-queue mode state (only used if you uncomment below) ----
        self._queue: asyncio.Queue[ProvisioningJob | None] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None

    # =========================================================================
    # submit() — the seam. Pick ONE of the two bodies below.
    # =========================================================================
    async def submit(self, job: ProvisioningJob) -> None:
        log.info(
            "queue.submit event_id=%s correlation_id=%s op=%s user_id=%s",
            job.event_id, job.correlation_id, job.op, job.user_id,
        )

        # --- MODE 1: INLINE (active) ----------------------------------------
        # The route waits for the service to finish before returning.
        if self._service is not None:
            await self._service.run(job)

        # --- MODE 2: ASYNC QUEUE (uncomment to enable, comment out MODE 1) --
        # await self._queue.put(job)
        # # submit returns now; the worker below will pick the job up.

    # =========================================================================
    # Worker loop — only runs in MODE 2. Lifecycle methods called from main.py.
    # Uncomment everything below to switch to background-worker processing.
    # =========================================================================
    # async def start(self) -> None:
    #     """Spawn the background worker. Call from FastAPI lifespan startup."""
    #     if self._worker_task is None:
    #         self._worker_task = asyncio.create_task(self._worker())
    #         log.info("queue worker started")
    #
    # async def stop(self) -> None:
    #     """Drain in-flight jobs and stop the worker. Call from lifespan shutdown."""
    #     await self._queue.put(None)  # poison pill
    #     if self._worker_task is not None:
    #         await self._worker_task
    #         self._worker_task = None
    #     log.info("queue worker stopped")
    #
    # async def _worker(self) -> None:
    #     """Pull jobs off the queue and run them, one at a time.
    #
    #     Single-consumer loop. If you want concurrency, spawn N of these
    #     in start() and they'll fan out across the same queue safely.
    #     """
    #     while True:
    #         job = await self._queue.get()
    #         if job is None:  # poison pill from stop()
    #             self._queue.task_done()
    #             break
    #         try:
    #             if self._service is not None:
    #                 await self._service.run(job)
    #         except Exception:
    #             # Don't let one bad job kill the worker. The connector_calls
    #             # row written by the service already records the failure.
    #             log.exception("job failed event_id=%s", job.event_id)
    #         finally:
    #             self._queue.task_done()
