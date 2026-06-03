# ProvisionHub

A SCIM 2.0 provisioning service between Microsoft Entra ID and downstream business apps (Slack, Jira).

> See **[PLAN.md](./PLAN.md)** for the full design, two-day schedule, and interview talking points.
> See **[CLAUDE.md](./CLAUDE.md)** for repo conventions and editing rules.

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
                                              ├─ Slack
                                              └─ Jira
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
