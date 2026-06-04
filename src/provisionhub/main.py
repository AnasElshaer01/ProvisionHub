"""FastAPI app entrypoint.

This is the *only* file that knows about concrete wiring: the JobQueue
singleton, eventually the ConnectorRegistry with Slack/Jira. Routes and
services stay generic.
"""

from __future__ import annotations

from fastapi import FastAPI

from .api import scim_users
from .api.middleware import CorrelationIdMiddleware
from .connectors.registry import ConnectorRegistry
from .db.session import SessionLocal
from .provisioning.queue import JobQueue
from .provisioning.service import ProvisioningService


def create_app() -> FastAPI:
    app = FastAPI(title="ProvisionHub", version="0.1.0")

    # Correlation-ID middleware runs before routes; routes read it off request.state.
    app.add_middleware(CorrelationIdMiddleware)

    # Wire the pipeline. Registry is empty for v1 — adding Slack/Jira is
    # one registry.register(...) line per connector (CLAUDE.md recipe).
    registry = ConnectorRegistry()
    # registry.register(SlackConnector(token=settings.slack_token))
    # registry.register(JiraConnector(base_url=settings.jira_base_url, token=settings.jira_token))

    service = ProvisioningService(session_factory=SessionLocal, registry=registry)
    queue = JobQueue(service=service)
    app.dependency_overrides[scim_users.get_queue] = lambda: queue

    app.include_router(scim_users.router)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
