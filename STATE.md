# ProvisionHub — Current State

Snapshot of where the codebase is, what's tested, what's deferred, and what to do next. Read this together with `PLAN.md` (scope / architecture) and `CLAUDE.md` (conventions).

Last updated: 2026-06-04 — background worker + parallel connector fan-out shipped.

---

## TL;DR

The full inbound pipeline is wired and tested. A `POST /scim/v2/Users` flows route → queue → service → registry → retry → audit, with all architectural seams intact and pinned by tests. The connector registry is empty, so no outbound HTTP happens yet — that's the next chunk of work (Slack + Jira).

```
git log --oneline
e271f8e feat: ProvisioningService closes the queue seam end-to-end
732e455 feat: connector ABC, registry, retry helper, and remote_id mapping
0950680 feat: SCIM /Users API with correlation tracking and the JobQueue seam
54716a8 feat: canonical user, SCIM wire models, and async SQLite storage
1ec2a55 chore: project bootstrap — package skeleton, deps, and runtime config
```

---

## What ships today

### Working surface

| endpoint | status |
|---|---|
| `POST /scim/v2/Users` | ✅ creates, idempotent by `externalId`, returns 201 (or 200 on re-POST) |
| `GET /scim/v2/Users` | ✅ supports the one filter `userName eq "x"` |
| `GET /scim/v2/Users/{id}` | ✅ 200 / 404 |
| `GET /healthz` | ✅ |
| `PUT /scim/v2/Users/{id}` | ❌ deferred |
| `PATCH /scim/v2/Users/{id}` | ❌ deferred |
| `DELETE /scim/v2/Users/{id}` | ❌ deferred |
| `GET /scim/v2/ServiceProviderConfig` | ❌ deferred |
| `GET /scim/v2/Schemas` | ❌ deferred |
| `GET /admin/events/{user_id}` | ❌ deferred |

### Working pipeline

```
SCIM POST  →  route writes users + provisioning_events  →  commit
            →  queue.submit(job)            ← puts on asyncio.Queue, returns
            →  route returns 201 to Entra (ms)
                              .
                              .  (background)
            →  worker pulls job
            →  service.run(job) opens its own session
            →  asyncio.gather over registry.enabled():    ← parallel across connectors
                  per connector: run_with_retry(connector.op(user))
                  one connector_calls row per HTTP attempt
                  upsert user_remote_ids on create-success
            →  per-connector failure: connector.exhausted ERROR log
            →  always: job.complete INFO log (outcome=success|partial|failure
                                              connectors=slack:ok,jira:failed)
```

### Architectural seams (locked by tests)

| seam | what's locked | test |
|---|---|---|
| Route → queue | route never calls `service.run` directly | `tests/test_queue.py::test_route_calls_queue_submit_not_service_directly` |
| Queue → service | `submit()` reaches `service.run` via the worker exactly once | `tests/test_queue.py::test_submit_routes_through_worker_to_service_run_once` |
| Worker resilience | one bad job doesn't kill the worker loop | `tests/test_queue.py::test_worker_survives_a_failing_job` |
| Service ↔ registry | service handles an unknown connector identically | `tests/test_service.py::test_service_handles_unknown_connector_identically` |
| Retry → audit | one `connector_calls` row per HTTP attempt | `tests/test_service.py::test_service_writes_one_call_row_per_retry_attempt` |
| Partial failure | one connector failing doesn't stop others | `tests/test_service.py::test_partial_failure_other_connectors_still_run` |
| Concurrent fan-out | connectors run at the same time, not sequentially | `tests/test_service.py::test_connectors_run_concurrently` |

### Test suite

```
pytest -q  →  7 passed in 0.52s
```

Retry tests use a monkeypatched `DEFAULT_BACKOFFS_S = (0, 0, 0)` (late-bound via `from . import retry as _retry`); production still uses (1s, 4s, 16s).

---

## File map (what's where)

```
src/provisionhub/
├── main.py                          create_app() — wires queue, service, empty registry
├── config.py                        Settings (DATABASE_URL, scim_bearer_token, slack/jira slots)
├── api/
│   ├── auth.py                      require_bearer dependency
│   ├── middleware.py                CorrelationIdMiddleware (X-Correlation-ID)
│   └── scim_users.py                POST + GET /scim/v2/Users
├── connectors/
│   ├── base.py                      Connector ABC: create_user, update_user, deactivate_user
│   └── registry.py                  ConnectorRegistry — register/enabled/get, dup raises
├── db/
│   ├── orm.py                       Base + 4 tables (users, provisioning_events,
│   │                                 connector_calls, user_remote_ids)
│   ├── session.py                   async engine + sessionmaker + get_session dep
│   ├── users_repo.py                UsersRepository — get/by_ext/by_name/list_/create/
│   │                                 update/set_active/delete (soft)
│   ├── events_repo.py               EventsRepository.record_inbound (insert-only)
│   ├── connector_calls_repo.py      ConnectorCallsRepository.record_attempt (insert-only)
│   └── remote_ids_repo.py           RemoteIdsRepository — get + upsert
├── domain/
│   ├── user.py                      Canonical User (flat) + Email (validated EmailStr)
│   └── scim.py                      SCIMUser/Name/Email + SCIMPatchOp + canonicalize()
└── provisioning/
    ├── job.py                       ProvisioningJob (event_id, correlation_id, user_id, op)
    ├── queue.py                     JobQueue — submit() puts on asyncio.Queue;
    │                                 _worker() consumes; start/stop via lifespan
    ├── retry.py                     run_with_retry, AttemptOutcome (with latency_ms), is_retriable
    └── service.py                   ProvisioningService.run — orchestrator;
                                      asyncio.gather across connectors (parallel)

alembic/
├── env.py                           render_as_batch=True, async engine
└── versions/
    ├── 0001_initial_schema.py       users + provisioning_events + connector_calls
    └── 0002_user_remote_ids.py      composite PK (user_id, connector) → remote_id

tests/
├── conftest.py                      in-memory SQLite (StaticPool), session/client fixtures
├── test_queue.py                    seam pinned from both sides
└── test_service.py                  extensibility proof + retry + partial failure

scripts/
└── smoke_db.py                      manual repo round-trip against real SQLite
```

---

## Decisions locked in (don't relitigate without reason)

1. **Canonical `User` is flat.** `id, external_id, user_name, active, given_name, family_name, emails: list[Email]`. Only `Email` is a nested model (validated via `EmailStr`). Connector-specific fields are forbidden here.
2. **SCIM PATCH `op` accepts mixed case** — Entra capitalizes (`Replace`). SCIM spec says case-insensitive.
3. **`emails` stored as JSON column** on `users`. No separate `emails` table. Tradeoff: no SQL filter by email. Acceptable for v1.
4. **Repositories don't commit.** Callers (route, service) own the transaction so multi-table writes stay atomic.
5. **Route commits BEFORE `queue.submit`.** The service's session can only see committed data. Since the service runs in a background worker, the route MUST commit before submitting or the worker would query against an empty transaction.
6. **`JobQueue` runs a background worker.** `submit()` puts the job on an `asyncio.Queue` and returns immediately; one worker task (spawned by the FastAPI lifespan) pulls jobs and calls `service.run`. Real broker (Redis/SQS) is the next swap behind the same `submit(job)` signature.
7. **Connectors run concurrently per job.** `ProvisioningService.run` calls `asyncio.gather` across the registry with `return_exceptions=True`. Each connector still does its own 3-attempt retry. Audit writes are serialized via an `asyncio.Lock` because the shared SQLAlchemy async session is single-consumer; the slow HTTP work is what's parallel.
8. **Three-method connector ABC.** Service maps SCIM `patch` → `update_user`. No fourth method.
8. **Update fallthrough → create.** If `op=update` arrives and we have no `remote_id` for that connector, run `create_user`. Self-healing for transient failures during initial provisioning.
9. **Partial failure preserved.** Connector failing after retries doesn't stop the rest. `connector_calls.succeeded=false` is the audit signal.
10. **`remote_id` lives in its own `user_remote_ids` table.** Composite PK (user_id, connector). Adding ServiceNow = rows, not schema.
11. **Retry: 3 attempts, (1s, 4s, 16s).** Retriable = httpx transport errors + 5xx + 429. Everything else (incl. 4xx) fails fast.
12. **Auth: static bearer token from `settings.scim_bearer_token`** (default `"dev-token"`). Real JWT/per-tenant is a v2 concern.
13. **SQLite + aiosqlite for v1.** Postgres is a `DATABASE_URL` env-var swap.
14. **Soft delete only.** `DELETE` flips `active=false`; row stays so audit FKs and re-PUT idempotency hold.
15. **No SCIM error envelope yet.** Routes raise plain `HTTPException`; FastAPI defaults are good enough for now. Wrap if Entra complains.

---

## Documented gaps (intentional)

- Groups (`/Groups`), ETag/`If-Match`, multi-tenant (`tenant_id`).
- Real broker behind `JobQueue` (the in-process `asyncio.Queue` is the bridge; swap to Redis/SQS is a body change in `queue.py`).
- Across-job parallelism (multiple background workers). Today one worker pulls; within-job fan-out across connectors is already parallel. Spawning N workers is one line in `JobQueue.start`.
- SCIM Bulk, complex filters (only `userName eq "x"` honored).
- Secrets manager — env vars only.
- `displayName`, `title`, enterprise extension URN — accepted via `extra="allow"`, not mapped.
- PUT/PATCH/DELETE routes.
- Admin endpoint joining `provisioning_events` ↔ `connector_calls`.
- SCIM error envelope (`schemas/status/detail/scimType`).
- `ServiceProviderConfig` + `Schemas` discovery endpoints.

---

## How to run it

```bash
# install
pip install -e ".[dev]"

# migrate
alembic upgrade head

# run
uvicorn provisionhub.main:app --reload

# test
pytest -q

# quick smoke against real SQLite (not the test suite)
python scripts/smoke_db.py
```

Manual route smoke:
```bash
curl -X POST http://127.0.0.1:8000/scim/v2/Users \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/scim+json" \
  -d '{"schemas":["urn:ietf:params:scim:schemas:core:2.0:User"],
       "externalId":"e1","userName":"alice@x.com"}'
```

---

## Next steps (in priority order)

### 1. Slack connector — `connectors/slack.py`
Slack supports SCIM directly (`scim.api.slack.com/v1/Users`). Mostly a passthrough — translate canonical User to SCIM body, POST/PATCH, parse `id` out of the response. Hardest part: looking up by `externalId` first (idempotency invariant).
- Add `SlackConnector(Connector)` in `connectors/slack.py`.
- Add `slack_token` use in `main.py`: `registry.register(SlackConnector(token=settings.slack_token))`.
- Add `tests/test_slack.py` with respx mocking the SCIM endpoints.
- Wire env in `.env` for local runs.

### 2. Jira connector — `connectors/jira.py`
Jira does NOT speak SCIM for users (Cloud has a SCIM endpoint but it's enterprise-gated). Use the REST `/rest/api/3/user` shape — that's where the canonical model proves its worth (the mapping is non-trivial).
- `JiraConnector(Connector)` with `base_url + token`.
- Lookup by `accountId` derived from `externalId` or by `emailAddress`.
- `tests/test_jira.py` with respx.

### 3. Admin endpoint — `api/admin.py`
`GET /admin/events/{user_id}` returns the user + all their `provisioning_events` joined with each `connector_calls` row. One query, one nested response shape. The debuggability story.

### 4. PUT / PATCH / DELETE routes
- PUT: full replace via `users_repo.update`, write `update` event, submit job with `op="update"`.
- PATCH: parse `SCIMPatchOp.Operations`, apply each (start with `active` and `userName`, document complex paths as gaps), submit `op="patch"`.
- DELETE: route calls `users_repo.delete` (soft), submits `op="delete"`.

### 5. Polish
- SCIM error envelope (`api/errors.py`) — small, focused commit.
- `ServiceProviderConfig` + `Schemas` discovery endpoints.
- `demo.http` file with the 5-minute demo sequence from `PLAN.md §10`.
- README updates.

### 6. Real broker (later)
Replace `asyncio.Queue` with Redis/SQS in `provisioning/queue.py`. The `submit(job)` signature, route, service, and connectors do not change. Adds durability across restarts and across-process scale-out.

---

## Known-not-broken oddities

- **`tests/conftest.py::client`** re-imports `create_app` per test to avoid override leakage AND wraps the client in `LifespanManager` so the background worker actually starts under `ASGITransport`. If integration tests start hanging on `queue.submit`, that's the first thing to check.
- **Idempotent re-POST returns 200** but the route's decorator says 201; we explicitly use `JSONResponse(status_code=200, ...)` for that one branch. Don't "simplify" by removing the JSONResponse.
- **`service.py` imports** `from . import retry as _retry`, not `from .retry import run_with_retry`. This is deliberate so `monkeypatch.setattr("provisionhub.provisioning.retry.DEFAULT_BACKOFFS_S", ...)` reaches it.
- **Async session is single-consumer**. The service's connector fan-out is via `asyncio.gather`, but `connector_calls` writes are taken under an `asyncio.Lock` for exactly this reason. Don't strip the lock to "simplify" the fan-out.

---

## How to resume

1. Read this file.
2. `git log --oneline` — confirms you're at `e271f8e` (or later).
3. `pytest -q` — should be 7/7 in <1s. If not, something broke since this snapshot was written.
4. Pick a "Next steps" item; usually #1 (Slack).
5. Use the same commit cadence: plan → code → smoke → one descriptive commit per logical chunk.
