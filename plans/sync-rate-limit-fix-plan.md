# Sync Rate-Limit Fix Plan

Stop the OneNote `429` storm by adding the request-**rate** limiter the current code is missing, and harden the retry layer so transient throttling/timeouts no longer turn into permanent page failures.

This plan fixes a regression introduced by `fbdff0d` ("perf(backend): parallelize sync pipeline") and completes the work `plans/sync-pipeline-concurrency-plan.md` only partially shipped.

---

## Root cause (confirmed)

`fbdff0d` turned a previously sequential sync into a parallel one: a module-level `asyncio.Semaphore(SYNC_GRAPH_CONCURRENCY=5)` in `graph_client.py:50`, fed by `SYNC_PAGE_WORKER_CONCURRENCY=10` page workers (`sync_service.py:376`). It implemented a **concurrency cap** but **no request-rate cap**.

A concurrency cap of 5 does not bound throughput. With sub-second `$value` resource fetches, 5 in-flight requests sustain roughly **300–600 requests/minute**, which is 2.5–5× OneNote's documented per-minute limit and blows the hourly cap within a single large notebook. The evidence:

1. **Almost every `429` in the logs is on `…/onenote/resources/…!…/$value`** — the per-resource image/ink download endpoint, the most aggressively throttled OneNote route. `_fetch_page_images` (`sync_service.py:465`) fires `asyncio.gather` over *every* image URL of a page, and 10 pages do this at once; all of it funnels through the 5-slot semaphore which stays saturated.

2. **Request volume per notebook exceeds the hourly cap by itself.** Per sync: 1 (notebook discovery) + 1/notebook (sections) + 1/section (page list) + per synced page `get_page_content` (1) + InkML (1 if handwriting) + N image `$value` fetches. A 5-section, 100-page notebook averaging 3 images/page ≈ **~406 requests** for one notebook — already at the 400/hour cap, fired as a burst.

3. **`retry_after=-` in every log line** confirms OneNote sends no `Retry-After` (see limits below), so the `wait_random_exponential(multiplier=1, max=60)` backoff in `_get` (`graph_client.py:259`) starts sub-second (`next_sleep_s=0.1…0.9`) and just re-probes a throttled service.

4. **Transport timeouts fail pages immediately.** `_is_retryable` (`graph_client.py:53`) only matches `httpx.HTTPStatusError`. The `httpx.ReadTimeout` that killed page *"Tutorial 4"* in the traceback is **not** retried — once the service is overloaded these spike and pages drop on the first timeout.

5. **Retry budget too small + failure amplification.** `_MAX_RETRIES=5` cannot ride out a throttling window → `Graph API unavailable — failed after 5 attempts` → pages marked `FAILED`. `_sync_section_metadata` (`sync_service.py:352`) force-resyncs `FAILED` pages next run, so failures compound load on the following sync.

The prior plan called this out explicitly — *"Do **not** raise Graph above 5 without a proper rate limiter and production telemetry"* (`sync-pipeline-concurrency-plan.md:53`). The rate limiter was never built; the concurrency cap alone is not sufficient.

---

## Official limits (confirmed)

Microsoft removed OneNote from the consolidated [service-specific throttling limits](https://learn.microsoft.com/en-us/graph/throttling-limits) page, but the documented OneNote delegated limits (and the values already recorded in `sync-pipeline-concurrency-plan.md:13`) are:

| Limit | Value | Scope |
|---|---:|---|
| Request rate | **120 requests / 60 s** | per app, per user |
| Hourly cap | **400 requests / 3600 s** | per app, per user |
| Concurrent requests | **5** | per app, per user |
| Global Graph ceiling | 130,000 requests / 10 s | per app, all tenants (not binding here) |

Key facts that shape the fix:

- **OneNote does not return a `Retry-After` header on `429`** — confirmed by Microsoft guidance and multiple write-ups, and matches `retry_after=-` in the logs. We must run our own backoff; we cannot lean on the server to tell us how long to wait.
- The limits are **per app, per user** = per `MicrosoftConnection`. The in-process limiter is now keyed by connection id, so each connected Microsoft account gets its own window, cooldown, and concurrency cap inside the single executor.
- The **400/hour** cap is the true sustained ceiling (~6.7 req/s average is wrong — it's ~6.7 req/**min** sustained). The 120/min is a short-burst allowance. Real-world limits are reportedly more generous than documented, but we design to the documented numbers and expose them as config.

Sources: [Microsoft Graph throttling guidance](https://learn.microsoft.com/en-us/graph/throttling), [service-specific limits](https://learn.microsoft.com/en-us/graph/throttling-limits), [OneNote throttling Q&A](https://learn.microsoft.com/en-us/answers/questions/5453983/onenote-what-are-the-method-to-avoid-throttling), [Note Bridge: what the docs don't tell you about OneNote rate limiting](https://note-bridge.co/en/blog/microsoft-graph-rate-limiting-onenote).

---

## Phasing

This ships in two phases. **Phase 1 is urgent and self-sufficient for stopping the 429s within a single process; Phase 2 is required for correctness across processes** (web + cron) and to make sync durable.

- **Phase 1 — Rate limiter + retry hardening.** Add the missing request-rate limiter and harden retries inside `GraphClient`. Unblocks syncing immediately for the common case.
- **Phase 2 — Durable job queue + single executor.** Route *all* sync entry points (UI, cron, CLI) through a Postgres-backed `sync_jobs` table drained by one worker process. This is what makes the Phase 1 limiter correct when both the web server and the cron want to sync the same Microsoft account, and it permanently fixes stuck `SYNCING` rows.

Phase 1 does not obviate Phase 2 and vice-versa: the queue guarantees a *single executor*; the limiter governs the request *rate within* that executor. Both are needed.

---

## Phase 1 — Rate limiter + retry hardening

All requests still funnel through `GraphClient._get`, but the limiter state now lives in a module-level registry keyed by `MicrosoftConnection.id`, so every caller is governed per connection — consistent with the "limits live with the resource" principle from the prior plan.

> ⚠️ The keyed limiter registry is **process-local**. It correctly caps each connection inside a single process, but two executor processes still mean two independent registries for the same connection. Phase 2 closes that by collapsing Graph work to one executor.

### 1. Add a sliding-window request-rate limiter (the core fix)

OneNote imposes **two limits over two timescales** — burst up to ~120/min, but sustain no more than ~400/hr — so any proactive limiter must track both windows. A token bucket can do this but needs two buckets with continuous-refill math; a **sliding-window log** is the simpler equivalent and is the chosen design.

Module-level registry state keyed by `connection_id`: each budget owns a `deque` of monotonic request timestamps, an `asyncio.Lock`, and a per-connection concurrency semaphore. Acquired inside `_get` **before** the concurrency semaphore.

`acquire()` before each request:

1. Take the lock; `now = monotonic()`.
2. Evict timestamps older than 3600 s (deque never holds more than `SYNC_GRAPH_RATE_PER_HOUR` entries).
3. `count_minute` = entries newer than `now - 60`; `count_hour` = deque length.
4. If `count_minute < per_minute` **and** `count_hour < per_hour` → append `now`, release, proceed.
5. Else release the lock and `await asyncio.sleep` until the soonest slot frees — the oldest in-window timestamp `+ 60` (per-minute) or deque-head `+ 3600` (per-hour), whichever applies — then re-check.

```python
SYNC_GRAPH_CONCURRENCY: int = 5          # documented hard cap; the rate limiter is the real control
SYNC_GRAPH_RATE_PER_MINUTE: int = 120    # documented max; dial down (e.g. 110) if throttled at the window edge
SYNC_GRAPH_RATE_PER_HOUR: int = 400      # documented max; hard ceiling for large/first syncs
```

Dependency-free (`collections.deque` + `asyncio.Lock`), memory is a few hundred floats. Why this over a token bucket: the mental model is the literal limit ("≥120 in the last 60 s? ≥400 in the last hour?"); one structure covers both windows; and unlike a naive fixed-window counter it has no boundary-burst flaw (no 120-at-0:59 + 120-at-1:00 = 240-in-2s). Acquire order in `_get`: rate-limit slot → concurrency semaphore → HTTP call.

Concurrency is the documented hard cap of 5; with the rate limiter binding at ~120/min (~2/s) it rarely fills even a few slots, so 5 is safe and the limiter — not concurrency — governs throughput.

### 2. Adaptive connection-local cooldown on `429` (since there's no `Retry-After`)

Because OneNote won't tell us how long to wait, a single 429 must back off that **connection's budget**, not just the failing request (root-cause #5). Mechanism: one budget-local "paused-until" deadline that requests for the same `MicrosoftConnection` check at the single chokepoint.

Module-level state, guarded by the limiter's existing lock:

```python
_paused_until: float = 0.0   # monotonic; no request may start before this
_throttle_level: int = 0     # consecutive 429s → backoff length
```

The limiter's `acquire()` (top of `_get`, before the HTTP call) checks the pause **first**, then the sliding window:

```python
async with _lock:
    now = monotonic()
    if now < _paused_until:
        wait = _paused_until - now            # in cooldown — every request waits
    else:
        ... sliding-window check ...          # normal pacing
# sleep outside the lock, then re-check
```

Because `_get` is the only path to Graph and `acquire()` is its first step, no request can be sent while `now < _paused_until`. The pause is a single shared deadline, not a per-request flag.

On a 429/503 response:

```python
async with _lock:
    _throttle_level = min(_throttle_level + 1, MAX_LEVEL)
    cooldown = min(CAP, BASE * 2 ** (_throttle_level - 1)) + random.uniform(0, JITTER)
    cooldown = max(cooldown, parse_retry_after(resp) or 0)   # honor header if ever present
    _paused_until = max(_paused_until, monotonic() + cooldown)
```

That one write is observed by the failing request (on its tenacity retry), the other in-flight slots (at their next `acquire()`), and all future requests — so the pipeline goes quiet until the deadline.

- **Escalation / decay:** consecutive 429s grow the pause exponentially; after a quiet window (T seconds / N clean successes) `_throttle_level` decays toward 0 so throughput recovers. Without decay, one bad minute throttles forever.
- **No thundering herd on reopen:** when the pause lifts, waiters still pass the ≤`per_minute` window check, so they ramp rather than stampede; cooldown jitter spreads wakeups.
- **tenacity split:** tenacity keeps attempt-counting / stop-after-N; the actual *waiting* is centralized at the gate so the failing request doesn't sleep twice (make tenacity's wait read the remaining `_paused_until`, or set it near-zero).
- **In-flight caveat:** a call already on the wire when the 429 lands completes (if it also 429s, it just refreshes the cooldown); we pause at the next acquire, so at most the other 4 slots finish before all is quiet.
- **Optional proactive signal:** read `x-ms-throttle-limit-percentage` / `x-ms-throttle-scope` when present and pre-slow before a hard 429.

> All of §1–§2 state lives in **process memory** (module globals + `asyncio.Lock`). This is correct only if exactly one long-lived process makes Graph calls — see [Deployment & runtime assumptions](#deployment--runtime-assumptions).

### 3. Honor `Retry-After` when present (defensive)

If a `429`/`503` ever does include `Retry-After`, use it as the cooldown floor (cap it, e.g. ≤ 120 s) instead of the computed backoff. Cheap, correct, and future-proofs against other Graph endpoints.

### 4. Retry transport timeouts

Extend `_is_retryable` (`graph_client.py:53`) to also retry `httpx.TimeoutException` (covers `ReadTimeout`/`ConnectTimeout`/`PoolTimeout`) and `httpx.TransportError`/`httpx.ConnectError`. These are transient under load and currently fail a page on first occurrence.

### 5. Raise the retry budget for throttling

Bump `_MAX_RETRIES` (e.g. 5 → 8) and keep `wait_random_exponential(max=...)` aligned with the cooldown. With the rate limiter in place this should rarely trigger, but it ensures a page survives a single throttling window instead of being marked `FAILED` and amplifying the next run.

### 6. (Optional) bound per-page image fan-out

`_fetch_page_images` gathering all image URLs at once is harmless once the rate limiter is the binding constraint, but capping it (e.g. small per-page gather chunk) smooths bursts and bounds peak memory. Low priority — do only if telemetry still shows spikes.

---

---

## Phase 2 — Durable job queue (single Graph executor)

### Why it's required, not just nice-to-have

The Phase 1 limiter is process-local. Today the live Graph executors are:

| Entry point | Process | Graph calls today |
|---|---|---|
| `POST /{id}/sync` → `run_notebook_sync_background` | web | per-notebook content sync (BackgroundTask) |
| `POST /refresh` → `refresh_notebook_list` | web | names-only discovery, **synchronously in the request** |
| `python -m sync.run` → `service.run()` | cron (separate) | full sync of all enabled notebooks |
| MCP server | web | none — reads synced DB content only |

Two processes making Graph calls against one per-user budget = two independent limiters = up to 2× the documented rate. The only ways to enforce one budget are a *distributed* limiter (DB/Redis token bucket) or a *single executor*. The queue gives the single executor for free: every entry point becomes an **enqueue-only producer**, and exactly one **worker** drains the queue and is the sole code path that constructs `SyncService` / touches `GraphClient`.

**Single-executor invariant (must be documented and enforced):** *exactly one process may run the worker.* Canonical shape on Railway: a dedicated `worker` service (1 replica); the web service(s) and the cron are pure producers and scale freely. For local dev, run the worker as a standalone process (`python -m sync.worker`) — or, opt-in, as an in-process lifespan task in the web app (only valid when exactly one web replica runs). If you ever run the worker in the web process *and* scale web > 1, you reintroduce the multi-executor problem; SKIP LOCKED keeps the *queue* safe but each replica still has its own rate limiter.

### Schema: `sync_jobs`

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `kind` | enum | `notebook_content` \| `discovery` (names-only list refresh + fan-out) |
| `connection_id` / `user_id` | FK | which Microsoft account the work belongs to |
| `notebook_id` | FK, nullable | null for `discovery`; `ON DELETE CASCADE` |
| `status` | enum | `pending` \| `running` \| `succeeded` \| `failed` \| `cancelled` |
| `source` | enum | `manual` \| `auto` \| `cli` (observability + priority) |
| `priority` | int | manual > auto so a user click isn't stuck behind a bulk auto-sync |
| `attempts` / `max_attempts` | int | retry budget |
| `next_run_at` | timestamptz | scheduling + backoff (claim only when `<= now()`) |
| `lease_expires_at` | timestamptz | heartbeat for crash recovery |
| `last_error` | text | |
| `created_at` / `started_at` / `finished_at` / `updated_at` | timestamptz | |

Migration is clean — the local DB is being nuked, so no backfill.

### Dedup (explicit)

Partial unique index — **at most one active job per notebook per kind**:

```sql
CREATE UNIQUE INDEX uq_sync_jobs_active
  ON sync_jobs (notebook_id, kind)
  WHERE status IN ('pending', 'running');
```

Enqueue is `INSERT … ON CONFLICT DO NOTHING RETURNING id` — the return tells the caller whether a new job was created or an active one already existed. This single mechanism handles every dedup case:

- **UI spam-clicking sync** on one notebook → collapses to one job.
- **Auto-sync (cron) overlapping a queued/running manual job** → the cron's enqueue for that notebook is a no-op. *(This is the "auto sync running while something is in the table" case you flagged — it resolves to: enqueue is idempotent per active job.)*
- **Two producers racing** → the unique index + `ON CONFLICT` makes the insert atomic.

### Claiming (safe even with one worker)

```sql
SELECT id FROM sync_jobs
 WHERE status = 'pending' AND next_run_at <= now()
 ORDER BY priority DESC, created_at
 FOR UPDATE SKIP LOCKED
 LIMIT 1;
```

Then mark `running`, set `started_at`, `lease_expires_at = now() + lease`. `SKIP LOCKED` makes it correct even if a second worker is ever started (the queue stays safe; only the rate-limiter invariant requires single-process).

### Crash recovery (permanently kills stuck `SYNCING`)

- Worker refreshes `lease_expires_at` on a heartbeat while a job runs.
- A **reaper** (on worker startup + periodically) requeues orphans:
  ```sql
  UPDATE sync_jobs SET status = 'pending', next_run_at = now()
   WHERE status = 'running' AND lease_expires_at < now();
  ```
  (Decrement remaining attempts / mark `failed` once exhausted.) It also reconciles the owning notebook's `sync_status` so a killed worker never leaves a notebook stranded in `SYNCING` — exactly the failure mode that happened when the server was killed.

### Notebook status vs job status (single source of truth)

The **job row is the operational truth** of "is work scheduled / running"; the notebook keeps the **user-facing** `sync_status`. The worker drives `notebook.sync_status` on transitions (→ `SYNCING` on claim, → `FRESH`/`FAILED` on finish) — same writes as today, just owned by the worker instead of the request. "Queued but not yet started" is **derived** from the existence of a `pending` job (UI joins or a small endpoint), so we don't duplicate a `QUEUED` state into the notebook table. (Add a `QUEUED` notebook status only if the UI can't cheaply derive it.)

### Retries / backoff — replaces the Phase-1 amplification

On failure: if `attempts < max_attempts`, set `status='pending'`, `next_run_at = now() + backoff(attempts)` (exponential + jitter), record `last_error`; else `status='failed'`. This **replaces** the current behavior where `_sync_section_metadata` force-resyncs every `FAILED` page on the next full run (root-cause #5) — retry becomes controlled at the job level instead of amplifying load on the following sync.

### Entry-point rework

- **`POST /{id}/sync`:** replace `background_tasks.add_task(run_notebook_sync_background, …)` with an enqueue of a `notebook_content` job (`source=manual`, high priority). The existing `SYNCING` guard in `start_notebook_sync` becomes "enqueue if no active job" — the partial unique index enforces it. Still returns 202; client keeps polling.
- **Cron / `sync/run.py`:** stop calling `service.run()` (which makes Graph calls directly). The cron enqueues **one `discovery` job** (`source=auto`). The **worker** runs names-only discovery, then fans out a `notebook_content` job per *enabled* notebook (dedup'd). This keeps the cron a pure producer so the worker stays the only executor. (Keep `--notebook-id` as a convenience that enqueues a single job; optionally a `--run-inline` debug flag that bypasses the queue — usable *only* when no worker is running, else two executors.)
- **`POST /refresh`:** **stays inline** — decision: web runs at **1 replica** for now. Names-only discovery is 1–2 requests bounded by the in-process limiter, and keeping it synchronous preserves the "refresh and show me the list now" UX. ⚠️ If web ever scales beyond 1 replica, move refresh to a high-priority `discovery` job so its Graph calls aren't made by multiple un-coordinated replicas.

### Edge cases checklist

- **`sync_enabled` toggled off after enqueue:** worker re-checks `sync_enabled` (and notebook existence) at claim time; if disabled/deleted → mark job `cancelled`, skip (don't spend budget).
- **Notebook deleted (local):** `ON DELETE CASCADE` removes its jobs; a running job's `sync_single_notebook` already no-ops on a missing notebook.
- **Notebook deleted from Graph:** discovery deletes it locally (existing logic); cascade clears jobs.
- **Cancellation:** disabling sync / deleting a notebook cancels its `pending` jobs (`UPDATE … status='cancelled' WHERE notebook_id=? AND status='pending'`); a `running` job finishes or is cooperatively cancelled.
- **Worker down:** jobs accumulate durably and drain when it restarts (the point of the queue). In dev this means **the worker must be running** for syncs/refresh to process — document the dev command.
- **Idempotency:** sync work is already upsert-by-`onenote_id`, so re-running a job after a crash is safe.
- **Token acquisition:** the worker acquires the access token per `connection_id` at job start (existing `_acquire_token`); a `NEEDS_REAUTH` connection fails its jobs with a clear `last_error` rather than retrying forever.

---

## Deployment & runtime assumptions

The Phase 1 limiter + cooldown are **in-process shared memory** (module globals + `asyncio.Lock`). They are correct only under one assumption: **exactly one long-lived process, with one event loop, makes Graph calls.** This holds on Railway, but it's a real constraint — it does *not* hold on every platform, so it must be stated.

Railway topology this plan assumes:

| Service | Replicas | Role | Graph calls? | Limiter state |
|---|---|---|---|---|
| web (FastAPI) | N (scalable) | enqueue jobs, serve API/MCP | none\* | n/a |
| worker (`python -m sync.worker`) | **exactly 1** | sole executor | yes | owns it |
| cron | ephemeral per run | enqueue `discovery` | none | n/a |
| Postgres | — | queue + synced data | — | — |

\* `POST /refresh` stays inline (decision: **1 web replica**). It makes 1–2 Graph calls from the web process under its own limiter — fine at 1 replica. If web scales > 1, move refresh to a `discovery` job (see Entry-point rework).

Load-bearing invariants:

- **Worker = 1 replica, 1 process.** Two replicas — or a multi-process server (`uvicorn --workers 2`, gunicorn) — means two independent limiters = 2× the per-user rate = the 429s return. The queue itself stays safe (`FOR UPDATE SKIP LOCKED`), but the rate limiter does not. Pin the worker to one replica and run it single-process. (Parallelizing across notebooks buys nothing for a single account anyway — the per-user budget is global.)
- **Deploy strategy = recreate, not overlap.** A rolling deploy that briefly runs old+new worker = two executors for a few seconds = transient rate doubling. Use stop-then-start for the worker service.
- **State is ephemeral across restarts.** A redeploy/crash resets `_request_times` / `_paused_until`, so a fresh worker may burst up to `per_minute` immediately and forgets an in-progress cooldown. Harmless (a new window), but don't rely on limiter state surviving restarts; orphaned *jobs* are handled separately by the reaper.

### Why this would not work on serverless (e.g. AWS Lambda)

Your instinct is right — this design is **not** portable to Lambda/Functions as-is:

- **No persistent process** for a long-running worker loop or background `asyncio` tasks; Lambda freezes execution between invocations, so timers/loops don't run.
- **Horizontal auto-scaling** spins up many isolated execution environments, each with its own memory → many independent limiters, no shared window or cooldown. In-memory rate limiting is meaningless there.
- Porting would require **externalizing** the limiter (atomic token bucket in Redis/DynamoDB via Lua / conditional writes) and serializing execution (reserved concurrency = 1, or an SQS FIFO queue) — i.e. the "distributed limiter" listed in [Out of scope](#out-of-scope). Railway's persistent-container model is exactly what lets us keep the limiter simple and in-memory.

---

## Files to change

### Phase 1
| File | Change |
|---|---|
| `backend/app/core/config.py` | Add `SYNC_GRAPH_RATE_PER_MINUTE` (120), `SYNC_GRAPH_RATE_PER_HOUR` (400); keep `SYNC_GRAPH_CONCURRENCY` at 5. |
| `backend/app/clients/graph_client.py` | Add a module-level per-connection budget registry; each budget owns sliding-window limiter state, cooldown, and concurrency cap. Rework `_get` acquire order; extend `_is_retryable`; honor `Retry-After`; bump `_MAX_RETRIES`. |
| `backend/.env.example` (if present) | Document the new knobs. |

The per-connection rework also threads `connection_key=connection.id` through `sync_service.py` and the debug scripts so `GraphClient._get` can select the right budget.

### Phase 2
| File | Change |
|---|---|
| `backend/alembic/versions/…` | Migration: `sync_jobs` table + partial unique dedup index. |
| `backend/app/models*` | `SyncJob` model + `SyncJobStatus`/`SyncJobKind` enums. |
| `backend/app/repositories/sync_job_repository.py` | Enqueue (`ON CONFLICT DO NOTHING`), claim (`FOR UPDATE SKIP LOCKED`), heartbeat, reap, transition helpers. |
| `backend/sync/worker.py` | New single-executor worker loop (claim → run via `SyncService` → finalize/retry), heartbeat + reaper. |
| `backend/sync/run.py` | Reduce to a producer: enqueue `discovery` (or `--notebook-id`) instead of running `service.run()`; optional `--run-inline` debug bypass. |
| `backend/app/routers/notebooks.py` | `POST /{id}/sync` and `POST /refresh` enqueue jobs instead of doing work in-process. |
| `backend/app/services/sync_service.py` | Remove `run_notebook_sync_background`; worker owns lifecycle. `discovery` fan-out enqueues per-notebook jobs. |

---

## Testing

### Phase 1
- **Rate-limiter unit test:** drive N fake requests through the sliding-window limiter with a mocked monotonic clock; assert no 60 s window ever exceeds `per_minute` and no 3600 s window exceeds `per_hour`, and that a blocked request wakes exactly when the oldest in-window entry ages out.
- **Retryable classification:** assert `httpx.ReadTimeout` and `504` are retried, `404`/`401` are not.
- **Cooldown behavior:** with `httpx.MockTransport` returning `429` then `200`, assert all in-flight workers pause and the request eventually succeeds within the bumped budget.
- **`Retry-After` honored:** mock a `429` carrying `Retry-After: 2` and assert the cooldown floor is used.

### Phase 2
- **Dedup:** enqueue the same `(notebook_id, kind)` twice → second insert returns no row; only one active job exists.
- **Claim safety:** two concurrent claims against a one-row queue → exactly one gets the job (`SKIP LOCKED`).
- **Crash recovery:** insert a `running` row with an expired lease → reaper requeues it and reconciles `notebook.sync_status`.
- **Retry/backoff:** a failing job requeues with growing `next_run_at` until `max_attempts`, then `failed`.
- **Auto/manual overlap:** enqueue a manual job, then a `discovery` fan-out for the same notebook → no duplicate; manual job ordered ahead by priority.
- **`sync_enabled` gate / cancellation:** disabling sync cancels pending jobs; worker skips a claimed job whose notebook is now disabled/deleted.

### Integration smoke (post-DB-nuke)
- Reset the local DB, start the worker (`python -m sync.worker`), enqueue a single notebook from the UI and a `discovery` from `sync/run.py`; confirm zero `429 failed after N attempts`, all pages reach `FRESH`, and no notebook is left in `SYNCING`.

---

## Rollout

1. **Phase 1:** land config + limiter + retry changes behind the new defaults. Nuke local DB (per decision — stuck `SYNCING` rows and `FAILED` pages discarded), re-run a single-notebook sync, watch `graph_retry` frequency. This alone unblocks single-process syncing.
2. **Phase 2:** add the `sync_jobs` migration + repository + worker; convert the web routes and `sync/run.py` to producers; remove `run_notebook_sync_background`. Run the worker as the sole executor.
3. Verify the cross-process case: trigger a UI sync and a cron `discovery` at the same time → still inside one per-user budget, zero unexpected `429`s.
4. Defaults sit at the documented maximums (5 / 120 / 400), so the tuning direction is **down**: if telemetry shows 429s clustering at a window edge, dial `SYNC_GRAPH_RATE_PER_MINUTE` back (e.g. 110). Do **not** raise concurrency past 5; the rate limiter (within the single worker), not concurrency, is the primary control.
5. Production shape: deploy the worker as a dedicated 1-replica Railway service; web + cron are producers. Enforce the single-executor invariant.

---

## Out of scope

- Microsoft Graph `$batch` / JSON batching (real reduction in call count, but a larger redesign of the fetch layer — track separately).
- A **distributed** rate limiter (DB/Redis token bucket) — only needed if we ever allow more than one executor process. The single-worker design makes the process-local limiter sufficient, so this stays out of scope unless the single-executor invariant is dropped.
- Worker concurrency across independent connections / queue fairness for multi-account throughput. The limiter is already keyed per `MicrosoftConnection`; overlapping work across connections is a separate worker scheduling change.
