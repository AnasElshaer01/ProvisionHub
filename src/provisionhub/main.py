"""FastAPI app entrypoint.

This is the *only* file that knows about concrete wiring: the JobQueue
singleton, eventually the ConnectorRegistry with Slack/Jira. Routes and
services stay generic.
"""

from __future__ import annotations

from fastapi import FastAPI

from .api import scim_users
from .api.middleware import CorrelationIdMiddleware
from .provisioning.queue import JobQueue


def create_app() -> FastAPI:
    app = FastAPI(title="ProvisionHub", version="0.1.0")

    # Correlation-ID middleware runs before routes; routes read it off request.state.
    app.add_middleware(CorrelationIdMiddleware)

    # The JobQueue singleton. Service will be wired in here on Day 2.
    queue = JobQueue(service=None)
    app.dependency_overrides[scim_users.get_queue] = lambda: queue

    app.include_router(scim_users.router)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
