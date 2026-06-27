# Per-user Graph rate-limit rework

Status: **implemented**
Related: `plans/sync-rate-limit-fix-plan.md` (the original throttle fix — this builds on it)

## TL;DR

Microsoft throttles the OneNote API **per-app-per-user** — every connected user has their
own independent budget (~120/min, ~400/hr, 5 concurrent). Before this rework, our limiter was **process-global**:
one `_GraphRateLimiter` + one `asyncio.Semaphore` shared by all requests regardless of which
user they belong to. So today, two users syncing at once are forced to split a single budget
that Microsoft would actually have given them separately — and one user's 429 cooldown pauses
*everyone*. With a single test account this is invisible; the moment there are real users it's
a correctness + throughput bug.

This implementation reworks the limiter to be **keyed per connection**. Letting the worker drain multiple
connections concurrently is a related throughput concern, but it's **deferred** (see below) — with a
single Railway instance and effectively one active account today, the correctness fix is what matters.

## Original state (grounding)

All Graph traffic funnels through `GraphClient._get` (`backend/app/clients/graph_client.py`):

```python
await _rate_limiter.acquire()              # module-level global
async with _graph_semaphore:               # module-level global
    await _rate_limiter.wait_out_cooldown()
    response = await self._client.get(url, headers=self._headers(access_token))
if response.status_code in _THROTTLE_STATUS_CODES:
    await _rate_limiter.register_throttle(_parse_retry_after(response))
```

- `_rate_limiter = _GraphRateLimiter(per_minute, per_hour)` — **one instance, module scope** (line ~184).
- `_graph_semaphore = asyncio.Semaphore(SYNC_GRAPH_CONCURRENCY)` — **one instance, module scope** (line ~71).
- The code already *acknowledges* the limitation in comments:
  - line ~69: *"When multiple Microsoft accounts are supported, switch to a per-connection keyed semaphore."*
  - the limiter docstring: *"correct only while a single process makes Graph calls."*
- The request carries `access_token` but **no user/connection identity** the limiter can key on.
- `sync/worker.py` drains `sync_jobs` **one job at a time, serially** (`_claim()` then `await _execute()`).
  So even after we key the limiter per connection, the worker still runs one connection's notebook at a
  time and the per-connection budgets sit idle. That's a **throughput** limitation, not a correctness
  one — exploiting the budgets for parallelism is the deferred worker-concurrency work, out of scope here.

## The oversight, precisely

1. **Shared window** — N users syncing concurrently share one 400/hr window, so each effectively
   gets `400/N` per hour instead of their own 400. We over-throttle real users.
2. **Shared cooldown** — a 429 from user A arms `_paused_until`, which `wait_out_cooldown()` makes
   *every* in-flight request observe, including user B who is nowhere near their limit. One noisy
   account stalls all accounts.
3. **Shared concurrency** — the 5-concurrent cap is per-app-per-user on Microsoft's side, but we
   apply a single 5 across all users.
4. **Serial worker** — independent per-user budgets buy no throughput while the worker only runs one
   notebook at a time; throughput stays bounded by serialization, not by the budgets.

(1)–(3) are **correctness** and are what this plan fixes. (4) is a **throughput** concern that only
bites once multiple connections have queued work at once — deferred (see Deferred), since today
there's a single instance and effectively one active account.

## Goals / non-goals

**Goals**
- Each Microsoft connection gets its **own** rate window, cooldown, and concurrency cap.
- One user's throttle must **not** pause another user's requests.
- Keep the in-process, injected-clock-testable design (no Redis dependency yet).

**Non-goals (this pass)**
- **Worker concurrency across connections** (the "throughput half"). Deferred — see Deferred. The
  worker stays one-job-at-a-time; per-connection budgets are still correct, just not yet exploited for
  parallelism. Pure win to add later when multiple active accounts make it matter.
- Distributed/multi-replica limiting (Redis token bucket). Documented under Deferred;
  the single-process invariant still holds for now.
- Changing the OCR/image-fetch volume (covered by the attachment + image-skip work — separate plan).
- `$batch` (does not relieve throttling).

## Design

### Key the limiter per connection

Introduce a registry that hands out a `_GraphRateLimiter` **and** a concurrency `Semaphore` per
key. **Key = connection id** (one Microsoft account = one connection = one Graph budget). This is the
unit Microsoft actually throttles (the account = the token = the `MicrosoftConnection`); `user_id`
coincides with it only because the schema enforces 1:1 (unique `user_id`, and reconnect upserts the
same row, so `connection_id` is stable across token refresh *and* reconnect). Keying on `connection_id`
costs nothing today and stays correct if a user ever links multiple Microsoft accounts (each gets its
own budget) — so it's strictly ≥ `user_id` with no downside.

```python
class _GraphBudget:
    """One Microsoft connection's private rate window + cooldown + concurrency cap."""
    def __init__(self, per_minute, per_hour, concurrency, clock=time.monotonic):
        self.limiter = _GraphRateLimiter(per_minute, per_hour, clock)
        self.semaphore = asyncio.Semaphore(concurrency)
        self.last_used = clock()

class _GraphBudgetRegistry:
    def __init__(self, ...): self._budgets: dict[Key, _GraphBudget] = {}; self._lock = asyncio.Lock()
    async def get(self, key: Key) -> _GraphBudget: ...      # create-on-miss, bump last_used, amortized evict
    def _evict_idle(self, now) -> None: ...                 # drop budgets idle past the threshold (called from get)
```

Thread the key through the call path as an explicit per-call parameter: `_get(url, access_token, *,
key)`, with the public methods (`get_notebooks`, `get_sections`, `get_pages`, `get_page_content`,
`get_page_image`, `get_page_content_with_ink`) accepting and forwarding `connection_key`. The private
wrappers must forward it too: `_get_all` (used by `get_notebooks/get_sections/get_pages`) and
`_get_content_with_inkml` (the beta fetch behind `get_page_content_with_ink`) both call `_get`, so the
key has to thread through them or those routes silently miss their budget.

This is the right shape here because the methods already all take `access_token` — `connection_key` is
the same kind of per-connection value, so it rides as a sibling param. It keeps the shared
`GraphClient` singleton **stateless** (the key is just data flowing through, never bound onto the
shared object), is type-checker-enforced (a required param flags any call site or hop that forgot it),
and avoids capturing a refreshable `access_token` into a long-lived bound object. `SyncService` already
knows the connection, so plumbing it is a few lines in the one orchestrator that makes the calls. The
cost is parameter noise on the signatures; cheap given the single caller.

`_get` becomes:

```python
budget = await _registry.get(connection_key)
await budget.limiter.acquire()
async with budget.semaphore:
    await budget.limiter.wait_out_cooldown()
    response = await self._client.get(url, headers=self._headers(access_token))
if response.status_code in _THROTTLE_STATUS_CODES:
    await budget.limiter.register_throttle(_parse_retry_after(response))
```

The `_GraphRateLimiter` class itself is unchanged — we just stop sharing one instance.

**Idle eviction (lazy, amortized)**: a long-lived process otherwise accumulates a budget per
connection forever. Reclaim it **lazily on `get`** — no background task — but **amortize** the scan so
the hot path stays O(1): only do the full sweep when more than `SYNC_GRAPH_BUDGET_EVICT_INTERVAL_S`
has elapsed since the last one. Garbage is only ever created on `get` (a new connection), so cleaning
on `get` matches the shape of the problem; a quiet process creates no new entries, so there's nothing
a background sweep would reclaim that this misses.

```python
async def get(self, key: Key) -> _GraphBudget:
    async with self._lock:
        now = self._clock()
        if now - self._last_evicted > self._evict_interval:
            self._evict_idle(now)          # one O(N) pass, at most once per interval
            self._last_evicted = now
        budget = self._budgets.get(key)
        if budget is None:
            budget = self._budgets[key] = _GraphBudget(per_minute, per_hour, concurrency, self._clock)
        budget.last_used = now             # bump on EVERY get — protects active connections
        return budget
```

Two invariants that make this safe:

- **`last_used` is bumped on every `get`**, so a connection that is actively syncing (calls seconds
  apart) never goes stale; only a *finished* connection ages out.
- **Never evict an in-use budget.** Dropping a budget that still has an in-flight request (holding its
  semaphore or mid-cooldown) would let a concurrent caller recreate a fresh limiter → two limiters for
  one connection → the per-user rate is briefly doubled. The `last_used` bump already prevents this as
  long as `SYNC_GRAPH_BUDGET_IDLE_EVICT_S` is set **comfortably longer than the largest gap between a
  connection's calls** (cooldown cap is 60s, jobs run minutes → an idle threshold of ~15–30 min is
  safely past anything active). `_evict_idle` must enforce the threshold against `last_used`, not
  blindly clear the dict.

### Config knobs (new / changed)

- `SYNC_GRAPH_RATE_PER_MINUTE`, `SYNC_GRAPH_RATE_PER_HOUR`, `SYNC_GRAPH_CONCURRENCY` — now
  interpreted **per connection** (semantics change; values likely unchanged).
- `SYNC_GRAPH_BUDGET_IDLE_EVICT_S: float` — drop a connection's limiter after this much idle (set well
  above the largest gap between a connection's calls, e.g. ~15–30 min, so an active budget is never evicted).
- `SYNC_GRAPH_BUDGET_EVICT_INTERVAL_S: float` — minimum gap between amortized idle sweeps on `get`.

## Testing

- Reuse the injected-clock pattern (`_GraphRateLimiter(..., clock=fake)`).
- New tests: two keys throttled independently — throttling key A leaves key B's `acquire()`
  returning immediately; key A's window fills without affecting key B's count.
- Registry: create-on-miss; `last_used` bumped on every `get`; an entry idle past
  `SYNC_GRAPH_BUDGET_IDLE_EVICT_S` is dropped and re-`get` recreates fresh state.
- Eviction is amortized + safe: with the injected clock, assert the full scan runs at most once per
  `SYNC_GRAPH_BUDGET_EVICT_INTERVAL_S` (hot path stays O(1) between sweeps), and that a budget kept
  warm by repeated `get`s within the idle window is **never** evicted (active-connection guard).

## Rollout

1. Land the per-connection limiter behind the same single-process assumption (pure refactor of where
   the limiter lives; behavior identical for the single-account case → safe to ship and verify no
   regression).
2. Update `sync-rate-limit-fix-plan.md` deployment notes: invariant is now "one *process*," not
   "one *request at a time*."

## Decisions (resolved)

- **Key = connection id** (not user id). It's the unit Microsoft throttles and stays correct under
  future multi-account-per-user; no cost today (see "Key the limiter per connection").
- **Scope = the per-connection limiter only.** Worker concurrency across connections (the throughput
  half) is deferred until multiple active accounts make it worthwhile.
- **Distributed/Redis limiter = out of scope.** We run a single Railway instance, so the in-process
  single-executor invariant holds; the per-key interface is built so a Redis backend can slot in later
  without a rewrite (see Future: distributed limiting).
- **Eviction = lazy-on-access, amortized** (no background task). Reclaim idle budgets on `get`, but
  scan at most once per `SYNC_GRAPH_BUDGET_EVICT_INTERVAL_S` so the hot path stays O(1). Garbage is
  only created on `get`, so cleaning on `get` fits the problem; a quiet process makes no new entries.
  `last_used` is bumped on every `get`, and the idle threshold sits well above any active call gap, so
  an in-use budget is never evicted (which would otherwise double a connection's rate via a recreated
  limiter). See "Idle eviction (lazy, amortized)". *Low-stakes — the state is tiny, so this is cheap
  insurance against long-uptime churn, not load-bearing.*

No open questions remain.

## Deferred (future work)

Both deferred until there are multiple active accounts and/or replicas; neither blocks the
per-connection limiter, and both slot behind the interface this plan builds:

- **Worker concurrency across connections (throughput).** Move the worker from one-job-at-a-time to a
  small pool of concurrent slots with an *at-most-one-job-per-connection* rule: extend `_claim()`
  (already `FOR UPDATE SKIP LOCKED`) to skip connections that already have an in-flight job (`NOT
  EXISTS (… status='running' AND connection_id = …)`), run up to `SYNC_WORKER_JOB_SLOTS` `_execute`
  coroutines, and let independent connections overlap while each stays serial against its own budget.
  Preserves the single-executor invariant (still one process, one registry). Heartbeat/lease/reaper are
  per-job and unaffected. Worthless for a single account, so deferred.
- **Distributed limiting (multi-replica).** Replace the in-process window with a shared token bucket in
  Redis keyed by connection id (Lua script for atomic check-and-decrement, TTL = window) — same per-key
  model, just shared state. Only needed if we ever run >1 executor process; the single Railway instance
  keeps the in-process invariant valid for now.
