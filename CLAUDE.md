# ProvisionHub

A SCIM 2.0 provisioning service that sits between an identity provider (Microsoft Entra ID) and downstream business apps (Slack, Jira). Built as a 2-day take-home for a technical interview.

**Read `PLAN.md` first.** It is the source of truth for scope, architecture, data model, file layout, and the two-day implementation schedule.

---

## TL;DR

- Inbound: SCIM 2.0 `Users` from Entra → FastAPI route
- Route writes an inbound audit row, builds a `ProvisioningJob`, submits it to a `JobQueue`
- `JobQueue` v1 body runs jobs inline (loop); v2 would push to a broker
- `ProvisioningService` iterates `ConnectorRegistry` with retry, writes one `connector_calls` row per HTTP attempt
- Storage: SQLite via SQLAlchemy 2 async (Postgres swap = `DATABASE_URL` change)

---

## The Two Architectural Seams

These are the load-bearing abstractions. Do not collapse them.

1. **`provisioning/queue.py` — `JobQueue`**
   The SCIM route calls `queue.submit(job)`, never `service.run(job)`. `submit` puts the job on an in-process `asyncio.Queue` and returns; a background worker (started by the FastAPI lifespan) pulls jobs and calls `service.run`. The route returns 201 in milliseconds; the work happens off the request path. The next swap is replacing the in-process queue with Redis/SQS — same `submit(job)` signature; **route, service, connectors do not change**.

2. **`connectors/registry.py` — `ConnectorRegistry`**
   `ProvisioningService.run` iterates `registry.enabled()` and never names a connector. Connectors run **concurrently** via `asyncio.gather` — Slack and Jira make their HTTP calls at the same time, each with its own retries. `main.py` is the only file that knows Slack/Jira/etc. exist. Adding a connector = one new file in `connectors/` + one `registry.register(...)` line in `main.py`. Untouched: SCIM API, queue, service, retry, audit schema, canonical model.

`tests/test_service.py` registers a fake connector at runtime and asserts the service handles it identically — that test is the proof of the extensibility claim, not the docs.

---

## Tech Stack

- Python 3.12
- FastAPI + Pydantic v2
- SQLAlchemy 2 async + Alembic (with `render_as_batch=True` for SQLite-safe migrations) + `aiosqlite`
- httpx for outbound; respx for mocking in tests
- pytest + pytest-asyncio
- docker-compose runs the app only — SQLite is a file, no DB service

---

## Repo Layout

```
src/provisionhub/
├── main.py                # registers connectors + wires JobQueue
├── config.py              # DATABASE_URL default: sqlite+aiosqlite:///./provisionhub.db
├── api/                   # scim_users, admin, auth, errors (SCIM envelope)
├── domain/                # canonical User + SCIM Pydantic models + canonicalize()
├── provisioning/          # job.py, queue.py (seam), service.py, retry.py
├── connectors/            # base ABC, registry, slack, jira
└── db/                    # orm (portable JSON columns), session
tests/                     # SCIM, queue seam, service+fake connector, slack, jira, demo.http
```

---

## Conventions

- **Audit tables are insert-only.** `provisioning_events` (inbound) and `connector_calls` (outbound, one row per HTTP attempt). Never `UPDATE` or `DELETE` from app code.
- **Correlation ID** is set by middleware on every inbound SCIM request and threaded into the `ProvisioningJob` and every `connector_calls` row. Use it as the join key when debugging.
- **`JSON` columns are SQLAlchemy's portable `JSON` type**, not Postgres `JSONB`. Keeps SQLite/Postgres swap clean.
- **Connector mapping lives in the connector module.** Canonical `User` never grows a Jira-specific or Slack-specific field.
- **Idempotency:** lookup-by-`external_id` before create in every connector.
- **Retry:** 3 attempts, exponential backoff (1s, 4s, 16s). Each attempt writes its own `connector_calls` row with `attempt` and `succeeded`.
- **Single-tenant.** No `tenant_id` columns. Documented as a gap.

---

## What's Explicitly Out of Scope (v1)

Groups, ETag/`If-Match`, multi-tenant, real broker, SCIM Bulk, complex filters (only `userName eq "x"`), secrets manager. All documented in the README as "with a week / with a month" extensions.

---

## Running

```bash
# install
uv sync   # or: pip install -e .

# migrate
alembic upgrade head

# run
uv run uvicorn provisionhub.main:app --reload

# or
docker compose up

# test
pytest
```

---

## When Editing

- Touching the SCIM route? The route should still call `queue.submit(job)` — never `service.run`. Don't collapse the seam.
- Adding a connector? Follow the 4-step recipe in the README. Do not edit `provisioning/service.py` to add app-specific branches.
- Adding a column to `users`? Generate an Alembic migration with `render_as_batch=True` (it's set in `alembic/env.py`) — SQLite needs it for `ALTER TABLE`.
- Touching audit? It's insert-only. If you find yourself writing an `UPDATE` against `provisioning_events` or `connector_calls`, you're modeling something wrong.
