# ProvisionHub — Final Plan (SQLite + Queue Logic, Loop Today)

**One-sentence plan:** A FastAPI SCIM 2.0 service that accepts user lifecycle events from Entra, writes an inbound audit row, builds a `ProvisioningJob`, submits it to a `JobQueue` (loop today, broker tomorrow); the `ProvisioningService` iterates the `ConnectorRegistry` with retry and writes a `connector_calls` row per HTTP attempt — all backed by **SQLite via SQLAlchemy async**, swappable to Postgres with a connection-string change.

---

## 1. The Pitch (memorize)

> *"Entra speaks SCIM. Each downstream app speaks its own shape. I built a small FastAPI service that accepts SCIM, normalizes to one internal user model, submits a job to a queue, and the queue runs it through per-app connectors behind one interface. The queue runs jobs inline today but is a real seam — swapping in Redis is a body change in one class. Adding a third app like ServiceNow is one new connector file. Storage is SQLite for v1; the ORM is backend-agnostic, so Postgres is a connection-string swap."*

Three load-bearing points: **canonical model**, **queue seam**, **connector registry**. All three are interview-credible because they're backed by code and tests.

---

## 2. Architecture

```
Entra ID
   │  SCIM 2.0
   ▼
FastAPI route
   │  1. save canonical user (SQLite)
   │  2. write provisioning_events row (inbound audit)
   │  3. build ProvisioningJob(event_id, correlation_id, user_id, op)
   │  4. JobQueue.submit(job)        ◀── the queue seam
   ▼
JobQueue.submit(job)
   │  puts job on asyncio.Queue, returns immediately
   │  (next swap: push to Redis/SQS; route + service untouched)
   ▼  (background worker, started by FastAPI lifespan)
ProvisioningService.run(job)
   │  asyncio.gather across registry.enabled():       ◀── parallel
   │     per connector: run_with_retry(connector.<op>(user))   (3 attempts: 1s, 4s, 16s)
   │     write connector_calls row per attempt (audit writes under asyncio.Lock)
   │  → connector.exhausted log per failed connector
   │  → job.complete log line: outcome=success|partial|failure connectors=slack:ok,jira:failed
   ▼
ConnectorRegistry ──▶ SlackConnector ──▶ Slack API   ┐ both at once
                  ──▶ JiraConnector  ──▶ Jira API    ┘
                  ──▶ (ServiceNowConnector — one new file + one register line)
```

Two interfaces carry the design: **`JobQueue`** (decouples receive from process) and **`Connector` + `ConnectorRegistry`** (decouples processor from app count).

---

## 3. What Ships

- **SCIM 2.0 server** — `Users` only: POST/GET/PUT/PATCH/DELETE + `ServiceProviderConfig` + `Schemas` + one filter operator (`userName eq "x"`)
- **Bearer-token auth**
- **Canonical `User`** + SCIM ↔ canonical mapping
- **`ProvisioningJob`** dataclass — work as data
- **`JobQueue`** — real class, in-process `asyncio.Queue` + background worker (lifecycle via FastAPI lifespan)
- **`ProvisioningService`** — iterates registry with retry, writes audit per attempt
- **`Connector` ABC** + **`ConnectorRegistry`**
- **`SlackConnector`** and **`JiraConnector`** — real httpx code, respx-mocked tests
- **Audit: 2 tables** — `provisioning_events` (inbound), `connector_calls` (outbound, one row per attempt)
- **Admin endpoint** — `GET /admin/events/{user_id}` joins both tables
- **`/healthz`, OpenAPI, README, docker-compose** (app only; SQLite needs no service)
- **Tests:** SCIM route, `JobQueue.submit` seam, service iterates a fake connector, Slack + Jira respx tests

### Documented gaps
Groups, ETag/`If-Match`, multi-tenant, real broker, Bulk endpoint, complex filters, secrets manager.

---

## 4. Tech Stack

| Concern | Choice |
|---|---|
| Framework | FastAPI + Pydantic v2 |
| DB | **SQLite via `aiosqlite`** + SQLAlchemy 2 async + Alembic (with `render_as_batch=True` for SQLite-safe migrations) |
| HTTP | httpx |
| Tests | pytest + pytest-asyncio + respx |
| Container | docker-compose (app only — SQLite is a file) |
| Python | 3.12 |

**Why SQLite:** single-tenant, append-only audit, FK lookups only — no Postgres feature is needed. The ORM is backend-agnostic; a `DATABASE_URL` change swaps to Postgres. The deliberate cost is zero; the deliberate win is reviewers clone and run with one command.

---

## 5. Data Model (3 tables — same on SQLite or Postgres)

- **users** — `id` (uuid str), `external_id`, `user_name` (unique), `active`, `emails` `JSON`, `name` `JSON`, `created_at`, `updated_at`
- **provisioning_events** *(insert-only)* — `id`, `correlation_id`, `op` (`create|update|patch|delete`), `user_id` (FK), `payload` `JSON`, `received_at`
- **connector_calls** *(insert-only)* — `id`, `event_id` (FK), `connector` (`slack|jira`), `endpoint`, `request` `JSON`, `response_status`, `response` `JSON`, `latency_ms`, `attempt`, `succeeded`, `error`, `created_at`

Indexes: FK columns, `users.external_id`, `provisioning_events.correlation_id`.

SQLAlchemy's portable `JSON` type maps to SQLite `JSON` (TEXT) and Postgres `JSONB` automatically.

---

## 6. The Queue Seam (the interview centerpiece)

```python
# provisioning/queue.py
class JobQueue:
    """The seam between 'a job was created' and 'a job got processed.'

    Today: in-process asyncio.Queue + a single background worker task.
    Tomorrow: push to Redis/SQS; a worker process pulls and calls service.run.
    Same submit() signature either way."""

    def __init__(self, service: "ProvisioningService"):
        self._service = service
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None

    async def submit(self, job: ProvisioningJob) -> None:
        await self._queue.put(job)         # returns in microseconds

    async def start(self) -> None:         # called from FastAPI lifespan
        self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:          # called from FastAPI lifespan
        await self._queue.put(_SHUTDOWN)
        await self._worker_task

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            if item is _SHUTDOWN:
                return
            try:
                await self._service.run(item)
            except Exception:
                log.exception("job failed event_id=%s", item.event_id)
            finally:
                self._queue.task_done()
```

The SCIM route never calls `ProvisioningService` directly — it calls `queue.submit(job)`. Swapping the in-process queue for Redis is a **body change in one class**; the route, service, and connectors do not change.

`test_queue.py` pins this seam: it asserts a submitted job reaches `service.run` exactly once through the worker, and that a single failing job doesn't kill the worker.

---

## 7. The Connector Seam (the extensibility claim, validated by test)

```python
# connectors/base.py
class Connector(ABC):
    name: ClassVar[str]
    async def create_user(self, user: User) -> str: ...      # returns remote_id
    async def update_user(self, remote_id: str, user: User) -> None: ...
    async def deactivate_user(self, remote_id: str) -> None: ...
```

```python
# main.py — the ONE place that knows which connectors exist
registry = ConnectorRegistry()
registry.register(SlackConnector(token=settings.slack_token))
registry.register(JiraConnector(base_url=settings.jira_url, token=settings.jira_token))
# To add ServiceNow:
# registry.register(ServiceNowConnector(...))   # ← one line
```

```python
# provisioning/service.py — never names a connector
async def run(self, job: ProvisioningJob) -> None:
    user = await self.users.get(job.user_id)
    connectors = list(self.registry.enabled())
    db_lock = asyncio.Lock()                                   # audit writes serialized
    results = await asyncio.gather(                            # HTTP runs in parallel
        *[self._dispatch(c, job, user, db_lock) for c in connectors],
        return_exceptions=True,
    )
    # one log line summarizing per-connector outcomes
    log.info("job.complete event_id=%s outcome=%s connectors=%s", ...)
```

**Add-a-connector recipe (in README, verbatim):**
1. Create `connectors/<name>.py` implementing `Connector`.
2. Add one `registry.register(...)` line in `main.py`.
3. Add one config block in `config.py`.
4. Add `tests/test_<name>.py` with respx fixtures.

**Untouched:** SCIM API, queue, service, retry, audit schema, canonical model.

**`test_service.py`** registers a fake `ServiceNowConnector` alongside Slack/Jira and asserts the service handles it identically. **That test is the proof, not a claim.**

---

## 8. Repo Layout

```
provisionhub/
├── docker-compose.yml         # app only — no db service
├── pyproject.toml
├── README.md
├── alembic/                   # render_as_batch=True for SQLite
├── provisionhub.db            # gitignored
├── src/provisionhub/
│   ├── main.py                # registers connectors + wires JobQueue
│   ├── config.py              # DATABASE_URL default: sqlite+aiosqlite:///./provisionhub.db
│   ├── api/
│   │   ├── scim_users.py      # calls queue.submit(job)
│   │   ├── admin.py
│   │   ├── auth.py
│   │   └── errors.py          # SCIM error envelope
│   ├── domain/
│   │   ├── user.py            # canonical User
│   │   └── scim.py            # SCIM Pydantic + canonicalize()
│   ├── provisioning/
│   │   ├── job.py             # ProvisioningJob dataclass
│   │   ├── queue.py           # JobQueue — the seam
│   │   ├── service.py         # ProvisioningService — loops registry with retry
│   │   └── retry.py
│   ├── connectors/
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── slack.py
│   │   └── jira.py
│   └── db/
│       ├── orm.py             # portable JSON columns
│       └── session.py         # async engine + session factory
└── tests/
    ├── conftest.py            # :memory: SQLite per test
    ├── test_scim_users.py
    ├── test_queue.py          # pins JobQueue → service seam
    ├── test_service.py        # fake connector → extensibility proof
    ├── test_slack.py          # respx
    ├── test_jira.py           # respx
    └── demo.http
```

---

## 9. Two-Day Plan

### Day 1 — SCIM + Audit + Queue Seam (~7h)

| Block | h | Output |
|---|---|---|
| Repo, pyproject, FastAPI skeleton, bearer auth, **SQLite engine + aiosqlite**, docker-compose (app only) | 1 | `uv run uvicorn …` boots; `compose up` boots; no DB service needed |
| ORM + Alembic (with `render_as_batch=True`) for 3 tables | 0.5 | Schema in |
| Canonical `User` + SCIM Pydantic models + `canonicalize()` | 1 | Validation works |
| `Users` CRUD + PATCH + single-op filter + SCIM error envelope | 2 | Round-trip against SQLite |
| `provisioning_events` write + correlation id middleware | 1 | Inbound audited |
| `ProvisioningJob` + `JobQueue` (inline body) + route calls `queue.submit` | 1 | **Queue seam wired** |
| `ServiceProviderConfig` + `Schemas` + first SCIM tests with `:memory:` SQLite | 0.5 | Discovery passes, tests instant |

### Day 2 — Connectors + Service + Polish (~7h)

| Block | h | Output |
|---|---|---|
| `Connector` ABC + `ConnectorRegistry` | 0.5 | Interface locked |
| `ProvisioningService.run` — registry loop + retry (3 attempts: 1s, 4s, 16s) + writes `connector_calls` per attempt | 1.5 | Audit pipeline complete |
| `SlackConnector` (SCIM passthrough) + respx tests | 1.5 | First connector proven |
| `JiraConnector` (REST mapping) + respx tests | 2 | Non-SCIM mapping proven |
| `test_queue.py` (pins seam) + `test_service.py` (fake connector → extensibility proof) | 0.5 | Both architectural claims test-locked |
| `GET /admin/events/{user_id}` + `/healthz` | 0.5 | Debuggability |
| README + `demo.http` + rehearse | 0.5 | Interview-ready |

---

## 10. The 5-Minute Demo

1. `uv run uvicorn provisionhub.main:app` *(or `docker compose up`)* — **no DB to wait for**
2. `POST /scim/v2/Users` with an Entra-shaped payload → 201, correlation id in response header
3. `GET /admin/events/{user_id}` → 1 inbound row + 2 outbound rows (Slack + Jira) with distinct request shapes
4. `PATCH active=false` → second inbound event, two more outbound rows showing deactivation
5. Open `connectors/jira.py` → walk the mapping. *"This is why the canonical user exists — Jira's shape is nothing like Slack's."*
6. Open `provisioning/service.py` → *"This service never names a connector. It loops the registry. Adding ServiceNow = one new file in `connectors/` + one register line in `main.py`."*
7. Open `provisioning/queue.py` → *"The SCIM route never calls the service directly — it submits a job to this `JobQueue`. `submit()` puts the job on an in-process queue and returns; a background worker — started by the FastAPI lifespan — pulls jobs and runs them. That's why the POST returns in milliseconds even if Slack is slow. Swapping the in-process queue for Redis is a body change in this file. Route, service, connectors — none change."*
8. Open `tests/test_service.py` → *"Fake connector registered at runtime. Service handles it identically. Proof, not claim."*
9. Open `db/orm.py` → *"SQLite via SQLAlchemy async. JSON columns are portable. Postgres swap is a connection-string change — the ORM is backend-agnostic."*

Steps 6–9 are your four architectural beats: **connectors extensible**, **queue is a real seam**, **storage is portable** — each backed by code or a test.

---

## 11. Interview Talking Points

- **Why SCIM:** Entra speaks it natively; zero IdP-side code per new tenant.
- **Why a canonical user:** N IdPs × M apps becomes N + M integrations.
- **Why audit in a DB, not logs:** queryable, joinable, one endpoint answers "what did we send to Jira for user X."
- **Why a `JobQueue`:** receiving and processing have different lifecycles, failure modes, and scaling needs. The route returns 201 in milliseconds; the worker grinds through retries against flaky downstreams in the background. Today the worker is an in-process task; tomorrow it's a worker process behind Redis. Same `submit(job)` signature.
- **Why parallel connector fan-out (`asyncio.gather`):** Slack and Jira are independent downstream systems. Running them sequentially would double the worst-case latency for no reason. `gather(..., return_exceptions=True)` keeps the partial-failure semantics — one connector failing doesn't cancel the other — and the test `test_connectors_run_concurrently` pins the wall-clock behavior so a future refactor can't quietly revert it.
- **Why retry stays in-process:** transient HTTP failures are the 90% case; per-call backoff handles them. A broker is for *durability across restarts* — the reason to swap `JobQueue`'s body.
- **Why a connector registry:** the service shouldn't know app names. Registry makes adding apps mechanical, and `test_service.py` proves it.
- **Why SQLite:** single-tenant, append-only audit, FK queries only — no Postgres feature is needed. ORM is backend-agnostic; Postgres swap is a `DATABASE_URL` change. Deliberate cost: not demonstrating Postgres-specific features I don't need. Deliberate win: reviewers clone and run with one command.
- **Failure mode handled:** partial provisioning. Slack succeeds, Jira fails → user + Slack remote_id stored, Jira attempt visible in `connector_calls` with `succeeded=false`. Re-`PUT` from Entra retries naturally via `external_id` lookup.
- **Idempotency:** SCIM `externalId` is the key. Connectors look up before creating.
- **With a week:** real `JobQueue` body (Redis + worker entrypoint), Groups, ETag/`If-Match`, Prometheus metrics on service latency.
- **With a month:** reconciliation job (drift detection), policy engine for per-connector routing rules, second IdP behind a similar inbound interface, Postgres swap when multi-tenant or write volume justifies it.

---

## 12. Defaulted Decisions

| Decision | Default |
|---|---|
| Tenancy | Single-tenant |
| Storage | SQLite via `aiosqlite`; portable to Postgres |
| Delete | DELETE → `active=false` + downstream deactivate; audit preserved |
| Groups | Out of scope v1 |
| Filter | `userName eq "x"` only |
| ETag | Not implemented |
| Secrets | Env vars via `config.py` |
| Idempotency | SCIM `externalId` + connector lookup-before-create |
| Queue | `JobQueue` interface, inline impl; body swappable to Redis |
