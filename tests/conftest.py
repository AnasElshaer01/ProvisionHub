"""Test fixtures: async in-memory SQLite + AsyncClient with overrides.

We skip Alembic in tests (Base.metadata.create_all is faster and the
migration is independently smoke-tested via `alembic upgrade head`).

The engine uses StaticPool so all sessions in a test share the same
in-memory database — the service opens its own session and must see
what the route's session wrote.

The `client` fixture drives the app's lifespan via `asgi-lifespan`'s
`LifespanManager` so the JobQueue background worker actually starts
and stops around each test. Without this, `await queue.submit(job)`
would silently park the job on a queue that nobody is consuming.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from provisionhub.db.orm import Base
from provisionhub.db.session import get_session


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # one shared connection -> one shared in-memory DB
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture
async def session(session_factory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest_asyncio.fixture
async def client(session_factory) -> AsyncIterator[AsyncClient]:
    """FastAPI client with the in-memory DB injected and lifespan driven.

    Re-imports `create_app` per-test so dependency_overrides don't leak.
    `LifespanManager` runs startup (queue worker spawn) and shutdown
    (poison pill + drain) around the body of the fixture.
    """
    from provisionhub.main import create_app

    app = create_app()

    async def _override_get_session():
        async with session_factory() as s:
            try:
                yield s
            except Exception:
                await s.rollback()
                raise

    app.dependency_overrides[get_session] = _override_get_session

    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c
