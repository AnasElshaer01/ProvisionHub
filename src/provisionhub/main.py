"""FastAPI app entrypoint.

This is the *only* file that knows about concrete wiring: the JobQueue
singleton, eventually the ConnectorRegistry with Slack/Jira. Routes and
services stay generic.

The FastAPI lifespan starts the JobQueue's background worker on startup
and drains it on shutdown — see `provisioning/queue.py`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import scim_users
from .api.middleware import CorrelationIdMiddleware
from .config import settings
from .connectors.jira import JiraConnector
from .connectors.zendesk import ZendeskConnector
from .connectors.registry import ConnectorRegistry
from .db.session import SessionLocal
from .provisioning.queue import JobQueue
from .provisioning.service import ProvisioningService


def create_app() -> FastAPI:
    # one registry.register(...) line per connector (CLAUDE.md recipe).
    registry = ConnectorRegistry()
    # registry.register(SlackConnector(token=settings.slack_token))
    if settings.jira_base_url and settings.jira_email and settings.jira_api_token:
        registry.register(JiraConnector(
            base_url=settings.jira_base_url,
            email=settings.jira_email,
            token=settings.jira_api_token,
        ))
    if settings.zendesk_subdomain and settings.zendesk_email and settings.zendesk_api_token:
        registry.register(ZendeskConnector(
            subdomain=settings.zendesk_subdomain,
            email=settings.zendesk_email,
            api_token=settings.zendesk_api_token,
            org_id=settings.zendesk_org_id,
        ))

    service = ProvisioningService(session_factory=SessionLocal, registry=registry)
    queue = JobQueue(service=service)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await queue.start()
        try:
            yield
        finally:
            await queue.stop()

    app = FastAPI(title="ProvisionHub", version="0.1.0", lifespan=lifespan)

    # Correlation-ID middleware runs before routes; routes read it off request.state.
    app.add_middleware(CorrelationIdMiddleware)

    app.dependency_overrides[scim_users.get_queue] = lambda: queue

    app.include_router(scim_users.router)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
