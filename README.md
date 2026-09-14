# ProvisionHub

> **A resilient, asynchronous SCIM 2.0 provisioning engine that bridges enterprise Identity Providers (Microsoft Entra ID) with downstream SaaS platforms (Jira Cloud, Zendesk) — featuring decoupled background queues, pluggable connectors, and complete audit traceability.**

---

## Quick Start

```bash
# install
uv sync

# migrate
alembic upgrade head

# run
uv run uvicorn provisionhub.main:app --reload
```

Or:

```bash
docker compose up
```

---

## Architecture (one breath)

```
Entra ID ─SCIM─▶ FastAPI ─▶ JobQueue.submit ─▶ ProvisioningService.run
                                                     │
                                                     ▼
                                            ConnectorRegistry
                                              ├─ Jira
                                              └─ Zendesk
```

- **`JobQueue`** is the seam between *receiving* and *processing*. v1 runs jobs inline; v2 pushes to a broker. Body change in one file.
- **`ConnectorRegistry`** decouples the service from the app count. Adding ServiceNow = one new file + one register line.
- **Audit** lives in two insert-only tables (`provisioning_events`, `connector_calls`) — one inbound row + one outbound row per HTTP attempt.
- **Storage** is SQLite via SQLAlchemy async. Postgres swap = `DATABASE_URL` change.

---

## Add a Connector

1. Create `src/provisionhub/connectors/<name>.py` implementing the `Connector` ABC.
2. Add one `registry.register(<Name>Connector(...))` line in `main.py`.
3. Add one config block in `config.py` for credentials.
4. Add `tests/test_<name>.py` with respx fixtures.

Untouched: SCIM API, queue, service, retry, audit schema, canonical model.

---

## Known Gaps (v1)

Groups, ETag/`If-Match`, multi-tenant, real broker, SCIM Bulk, complex filters (`userName eq "x"` only), secrets manager.
