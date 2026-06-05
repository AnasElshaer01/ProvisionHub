# ProvisionHub — Decision Notes

Companion to `PLAN.md` (scope), `STATE.md` (current state), and `CLAUDE.md` (conventions). This file captures the **why** behind a few architectural choices that are easy to misread from the code alone.

If you find yourself wondering "why isn't this done the other way?" — start here.

---

## 1. Fan-out happens *inside* the service, not at the queue

### The question

When a SCIM request comes in and we have multiple connectors (Slack, Jira, …), how do we model the work? Two natural answers exist.

### Pattern A — fan-out at the queue (rejected)

The route submits **one job per connector**:

```
route ──► queue.submit(job{event=A, connector=slack})
       ──► queue.submit(job{event=A, connector=jira})

worker picks each independently and calls a single connector.
```

### Pattern B — fan-out inside the service (chosen)

The route submits **one job for the whole request**. The service loops the connector registry internally:

```
route ──► queue.submit(job{event=A, op=create, user=usr-1})
       ──► service.run(job)
              for connector in registry.enabled():
                  _dispatch(connector, job)
```

### Why Pattern B wins for ProvisionHub

The **load-bearing architectural promise** of this codebase is:

> Adding a connector = one new file in `connectors/` + one `registry.register(...)` line in `main.py`. Nothing else changes.

Pattern A breaks that promise the moment you add a third connector. Here's the comparison:

| Concern | Pattern A (per-connector job) | Pattern B (single job, service fans out) |
|---|---|---|
| **Who knows the connector list?** | Route or queue (to enqueue N jobs). The list leaks outward. | Only `main.py` + `ConnectorRegistry`. Route, queue, job model never name a connector. |
| **Audit shape** | "Event" becomes ambiguous — is it the inbound request or the per-connector attempt? You'd need two tables or an N-row event table per request. | One `provisioning_events` row per inbound request. `connector_calls` rows hang off it via `event_id`. Clean one-to-many. |
| **Adding a connector** | New file + register line + **change whoever fans out jobs** (route or a fan-out worker). | New file + register line. End of list. |
| **Partial-failure policy** | Distributed across whatever schedules per-connector jobs. Easy to drift. | Lives in exactly one place (`service.run`'s `try/except` inside the loop). One policy, applied uniformly. |
| **Retry policy** | Per-queue, per-connector — each connector could end up with its own retry semantics by accident. | One `run_with_retry` helper, one set of backoffs, applied uniformly. Tunable in one file. |
| **Independent parallelism per connector** | ✅ Free — each job is independent work. | ⚠️ Sequential by default. Would need `asyncio.gather` + a session-per-task to parallelize. |
| **Independent per-connector scheduling** (e.g. "Slack tier is slow, only run 1 concurrent") | ✅ Trivial via per-queue/per-worker config. | ⚠️ Requires per-connector semaphores inside the service. |
| **MODE 2 / real broker compatibility** | ✅ Each connector job is naturally independent across workers. | ✅ Still works — one job, one worker pulls it, service iterates. Just less parallel. |
| **Test surface** | Multiple jobs to construct/assert per request. | One job. The "service handles unknown connector identically" test (test_service.py) is the entire extensibility proof. |

### The honest trade-off

Pattern B is **sequential per request**. If Slack takes 200ms and Jira takes 300ms, your single job takes ~500ms wall-clock, not max(200, 300). Pattern A would have run them concurrently across workers.

We accept that cost because:

1. The fan-out is bounded (a handful of connectors per deployment, not hundreds).
2. Each connector internally is async — adding `asyncio.gather` inside `service.run` is a small future change, not a re-architecture.
3. The architectural simplicity (the "one file + one line" promise) is worth more than per-request latency at this scale.

### When you'd revisit this

Pattern A starts looking attractive when:

- You have 10+ connectors per deployment.
- Connectors have wildly different latencies (one is 50ms, another is 30s).
- You want per-connector concurrency limits enforced by infrastructure (different queues with different worker counts).
- You're running enough volume that "one job per request" creates a single hot worker.

At that point you'd introduce a **second seam**: the service emits per-connector follow-up jobs onto a secondary queue. The original `event_id` stays as the parent; the per-connector jobs reference it. You keep Pattern B as the entry point and add Pattern A as a refinement underneath, instead of letting Pattern A leak into the route.

---

## 2. What this means for SCIM Bulk

SCIM Bulk is explicitly out of scope for v1 (see README). When we add it, the design has to play nicely with the fan-out choice above.

### How SCIM Bulk works

One HTTP request from Entra carries up to N operations (e.g. "create 50 users"). The SCIM spec wraps them in a `BulkRequest` envelope; the response is a parallel `BulkResponse` envelope with per-operation status.

### How it fits Pattern B

The natural mapping is:

```
ONE bulk HTTP request
   │
   ├─► ONE provisioning_events row PER operation       (50 rows)
   ├─► ONE ProvisioningJob PER operation               (50 jobs)
   │
   │  all 50 share the SAME correlation_id (the request)
   │  each has its OWN event_id (its own audit row)
   │
   ▼
queue.submit(job) called 50 times
   │
   ▼
service.run(job) called 50 times — each fans out to all connectors
```

So **per-operation, Pattern B is unchanged**. The bulk envelope just expands one HTTP call into N route-level loop iterations.

This is the **only** place where one request legitimately produces multiple `event_id`s — and it's by design: each user operation in the bulk is its own auditable unit. The `correlation_id` glues them back together as "the same Entra batch."

### What changes

- The route grows a `parse_bulk → for each op: do the existing thing`. Not a re-architecture, a loop.
- Response shaping changes — we need to return per-operation status in the `BulkResponse`.
- We commit-per-op (or batch-commit) so a partial failure doesn't blow away successful ops earlier in the batch.
- Idempotency keys (`bulkId` references between operations in the spec) need handling. That's the genuinely hard part.

### What does NOT change

- The job model (`ProvisioningJob`).
- The queue (`JobQueue.submit`).
- The service (`ProvisioningService.run`).
- The connector ABC.
- The retry helper.
- The audit table shape.

That's the whole point of having picked Pattern B: bulk slots in cleanly at the route layer without disturbing anything below it.

### Why Pattern A would have made Bulk worse

Under Pattern A, a 50-user bulk with 2 connectors becomes **100 jobs in the queue**, none of which know they came from the same SCIM bulk. Reconstructing the bulk response — "did *operation 17* succeed across all its connectors?" — becomes a query nightmare. Under Pattern B, "did operation 17 succeed?" is one `event_id` and its child `connector_calls` rows. The bulk response is N event-level summaries; each summary is computed locally.

---

## 3. `correlation_id` vs `event_id` — the difference, made concrete

This is the single most confused pair of IDs in the codebase. They look like they do the same job. They don't.

### The one-sentence distinction

- **`correlation_id`** = a label on the **request** that travels with it, including to and from systems we don't own (Entra, logs, dashboards).
- **`event_id`** = a primary key on a **row** in our `provisioning_events` table, owned entirely by us.

### Side-by-side

| Property | `correlation_id` | `event_id` |
|---|---|---|
| **Who creates it** | Entra (sent in `X-Correlation-ID`), or our middleware if missing | `events_repo.record_inbound` via `uuid.uuid4()` |
| **Where it lives** | Request headers, response headers, our DB column, future Redis job body, future log/Sentry tags — **everywhere** | One column: `provisioning_events.id` |
| **Visible to the caller?** | Yes — we echo it in the response header | No — it's an internal PK |
| **Unique?** | No (not enforced). Just an index. Two requests *could* share one. | Yes (primary key) |
| **Survives a worker hop?** | Yes — it's in the job body and in logs | Yes — it's also in the job body, but only meaningful inside our DB |
| **Cardinality per inbound HTTP request** | 1 | 1 normally; **N for a SCIM Bulk request** |
| **What you query with** | "Find me the story of that request" (discovery) | "Drill into this one audit row" (precise lookup) |
| **What you'd lose by removing it** | Cross-system traceability dies. Entra's logs no longer map to ours. | Foreign-key relationship between events and connector_calls breaks. The whole audit table collapses. |

### Why the confusion exists

In the common case (one SCIM request, not a bulk), they're 1-to-1: one `correlation_id`, one `event_id`. They look interchangeable. The distinction only becomes visible at two boundaries:

1. **The system boundary.** Entra knows the `correlation_id` and not the `event_id`. So any conversation with Entra (or with the caller's logs) has to use the correlation_id.
2. **The bulk boundary.** One bulk request = one `correlation_id` = **N** `event_id`s. Now they're 1-to-N and you absolutely cannot use them interchangeably.

So in v1, treat them as "two IDs that mostly behave the same but are about to diverge the moment Bulk lands." Building the abstraction now means Bulk costs us nothing later.

### Working example — the regular case (no bulk)

Entra sends `POST /scim/v2/Users` with header `X-Correlation-ID: 7c9e6a2b-...`.

```
correlation_id  = 7c9e6a2b-...        ← given by Entra
event_id        = evt-abc123          ← we minted

provisioning_events:
  id             = evt-abc123
  correlation_id = 7c9e6a2b-...

connector_calls (3 rows for one failed Slack retry burst):
  event_id       = evt-abc123     (×3)
```

1-to-1, easy.

### Working example — a SCIM Bulk request

Entra sends one `POST /scim/v2/Bulk` containing 3 user creates, with header `X-Correlation-ID: bulk-9999-...`.

```
correlation_id  = bulk-9999-...      ← ONE id for the whole bulk
event_ids       = evt-a, evt-b, evt-c  ← THREE ids, one per operation

provisioning_events:
  id=evt-a, correlation_id=bulk-9999-...
  id=evt-b, correlation_id=bulk-9999-...
  id=evt-c, correlation_id=bulk-9999-...

connector_calls (assuming 2 connectors, all 1-shot successes):
  event_id=evt-a, connector=slack
  event_id=evt-a, connector=jira
  event_id=evt-b, connector=slack
  event_id=evt-b, connector=jira
  event_id=evt-c, connector=slack
  event_id=evt-c, connector=jira
```

Now the distinction is impossible to miss:

- **"Show me everything from that bulk request"** → `WHERE correlation_id = 'bulk-9999-...'` — returns all 3 events and (via join) all 6 connector_calls.
- **"Show me the calls for the second user in the bulk"** → `WHERE event_id = 'evt-b'` — returns 2 connector_calls, scoped to that one operation.

The correlation_id is the **batch**. The event_id is the **operation inside the batch**. In the non-bulk case, the batch is a batch of one and they coincide — but the abstraction is already there in the schema.

### How to think about it going forward

- **If a caller could ever say it to you, it's a correlation_id.** ("My request from 9:42…", "Entra logged this id…", "The customer's ticket says…")
- **If only your DB knows it, it's an event_id.** (FK targets, audit drilldowns, internal admin views.)
- **You query `correlation_id` to discover; you join on `event_id` to drill in.**
- **They are 1-to-1 today and 1-to-N the moment Bulk ships. Don't conflate them in code now or you'll pay later.**

### The slogan

> `event_id` is local. `correlation_id` is global.
>
> `event_id` is a row. `correlation_id` is a story.

---

## 4. The queue is a background worker; connectors fan out concurrently

### The question

Two related decisions that got bundled together once we admitted the queue's whole reason to exist is the background worker:

1. **Should the queue run jobs inline or in the background?**
2. **Once a job is being processed, should the connectors run sequentially or at the same time?**

### What we chose

1. **Background worker.** `queue.submit(job)` puts the job on an `asyncio.Queue` and returns. The FastAPI lifespan starts a single worker task that pulls jobs and calls `service.run`. The route returns 201 in milliseconds; retries against flaky downstreams happen off the request path.
2. **Concurrent fan-out inside `service.run`.** When the worker picks up a job, it calls `asyncio.gather` over `registry.enabled()` so Slack and Jira make their HTTP calls **at the same time**. Each connector still runs its own 3-attempt retry. Audit writes against the shared session are taken under an `asyncio.Lock`.

### Why this shape

| Concern | Sequential (inline or sequential gather) | Parallel fan-out (what we do) |
|---|---|---|
| **Worst-case latency** | sum(connector latencies) | max(connector latency) |
| **Slow downstream blocks others** | yes — Slack timing out delays Jira | no — they run in parallel |
| **Partial-failure semantics** | preserved | preserved (`return_exceptions=True`) |
| **Connector list leaks outward** | no | no — Pattern B still holds |
| **DB safety** | trivial (one writer) | requires `asyncio.Lock` around audit writes |
| **Code change** | for-loop | for-loop → `asyncio.gather` (~15 lines) |
| **Failure-reporting clarity** | one log per job | one log per failed connector + one job summary |

The Pattern B decision in §1 said "fan out *inside* the service, not at the queue." Concurrency is the second half of that decision — we always wanted Slack and Jira to be independent; sequential was just the cheapest correct shape. Switching the loop to `gather` keeps Pattern B intact (still one job per request, still no per-connector queues) and removes the only real performance reason to ever reach for per-connector workers at this scale.

### The DB-lock detail

SQLAlchemy async sessions are **single-consumer**. If two concurrent `_dispatch` tasks tried to write `connector_calls` rows on the shared session at the same time, the session would explode (`IllegalStateChangeError` or worse). The fix is small and contained: an `asyncio.Lock` constructed in `run`, passed to `_dispatch`, and held only around the audit writes. The HTTP work — `run_with_retry` calling out to Slack/Jira — happens **outside** the lock, which is where the parallelism we want actually lives.

A note for future maintainers: **don't try to "simplify" by removing the lock.** It looks like dead code if you don't know SQLAlchemy's threading model. If you want true parallel writes, give each task its own session — but that's a much bigger change than this commit's scope.

### Per-job reporting

Two log lines do the operator-visible reporting on top of the (verbose) `connector_calls` table:

- `connector.exhausted connector=jira event_id=... error=...` — one ERROR line per connector that gave up.
- `job.complete event_id=... correlation_id=... op=create outcome=partial connectors=slack:ok,jira:failed` — one INFO line per job, summarizing the outcome.

`outcome` is `success`, `partial`, or `failure`. The `connectors=` field is grep-friendly and pipes naturally into any log aggregator. No metrics dependency, no new schema, no extra endpoint — and the existing `connector_calls` rows are still there for forensic detail.

### Durability gap (honestly)

The queue is in-process. If the host dies with jobs queued, those jobs are **lost**. The `provisioning_events` row was committed by the route before submit, so the inbound audit is durable — but the outbound attempts for in-flight jobs are gone. Closing this gap is what a real broker (Redis, SQS) is for, and the seam is exactly `queue.submit` — swapping the broker in later is a body change in `queue.py` and nothing else.

### Where you'd revisit this

Spawn N worker tasks in `JobQueue.start()` if **across-job** parallelism becomes the bottleneck (many concurrent requests, each fanning out). `asyncio.Queue` is N-consumer safe; the only thing to remember is that the service's audit lock is per-job, so each job's writes serialize but jobs themselves run independently. That's a single-line change in `start()` plus a settings field for the worker count.

Per-connector queues (Pattern A) only become attractive at a much larger scale — when one connector's sustained latency would clog the shared queue. We document that as a future evolution in §1's "When you'd revisit this."

---

## TL;DR

1. **One SCIM request = one event = one job.** Multiple connectors are iterated **inside** `service.run`, not by submitting multiple jobs. This preserves the "add a connector = one file + one line" promise (Pattern B over Pattern A).
2. **SCIM Bulk slots in at the route only.** It expands one HTTP call into N events/jobs but doesn't touch the queue, service, retry, or connector layers. The Pattern B choice is what makes that cheap.
3. **`correlation_id` is the request (1, given by the caller). `event_id` is the audit row (1 normally, N for bulk). They look the same in v1 because the bulk case isn't here yet — but the schema already distinguishes them, and ignoring the distinction now would break the moment Bulk lands.**
4. **Queue is a background worker; service runs connectors concurrently with `asyncio.gather`.** Route returns 201 in ms; Slack and Jira go at the same time. Audit writes are serialized by an `asyncio.Lock` (the shared session is single-consumer); HTTP fan-out is the slow part that runs in parallel. Per-job log lines (`connector.exhausted`, `job.complete`) report outcomes. In-memory queue = lossy on crash; real broker is the next swap behind the same `submit(job)` signature.
