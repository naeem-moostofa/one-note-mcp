# Sync Stale-Delete Data Loss

A separate, higher-severity bug from the OOM/throttle stopgap (`sync-worker-stopgap-stabilization.md`): local pages — and potentially whole sections/notebooks — can be **silently deleted** from Postgres when Microsoft Graph returns an empty or partial list response. This is *data loss*, not degraded sync, and it can happen on a single bad response even when the sync job later fails.

This plan documents the root cause, what the Microsoft Graph docs actually guarantee (the "all or nothing" assumption is wrong), and the guarded-reconciliation fix.

> **Status (2026-06-27):** Patches **A** (completeness-gated deletes via `@odata.count`) and **B** (explicit `$skip` paging) are **implemented**. Patch **C** (soft-delete/tombstones) was intentionally skipped. Patch **D** (delta) remains strategic/future. The `_get_all` paging + completeness logic was validated against a mocked Graph transport across six cases (>100 paging, degraded partial 200, count-absent, genuine-empty, empty-no-count, exact page boundary).

---

## Symptom

After a sync run under throttling / OOM-restart pressure, pages that still exist in OneNote were gone from the local DB.

---

## Root Cause

Sync reconciles local state to Graph with an **"upsert + delete-stale"** pattern at three levels. Each level treats whatever Graph returns as **authoritative ground truth** and hard-deletes anything not present in that response — and commits the delete immediately.

### The unguarded enumerator

`GraphClient._get_all()` (`backend/app/clients/graph_client.py:601-617`) builds the list from `data.get("value", [])` and stops when `@odata.nextLink` is absent:

```python
items: list[_M] = []
next_url: str | None = url
while next_url:
    response = await self._get(next_url, access_token, connection_key=connection_key)
    data = response.json()
    for item in data.get("value", []):
        items.append(model.model_validate(item))
    next_url = data.get("@odata.nextLink")
return items
```

A `200 OK` with an **empty or partial** `value` array returns a short/empty list **without raising**. There is no completeness check. (Sustained `429`s *do* raise after retries and are safe — they never reach the delete path. The danger is a *successful* but degraded `200`.)

### The three delete-stale trigger points

| Level | Code | Trigger | Blast radius |
|---|---|---|---|
| Notebooks | `sync_service.py:225-229` | `get_notebooks()` empty/partial | `delete_many` → **CASCADE wipes sections → pages** |
| Sections | `sync_service.py:323-327` | `get_sections()` empty/partial | `delete_many` → **CASCADE wipes pages** |
| Pages (empty) | `sync_service.py:388-393` | `get_pages()` returns `[]` | deletes **every page in the section** |
| Pages (partial) | `sync_service.py:405-409` | `get_pages()` returns a short list | deletes every page whose id isn't in the returned set |

Cascade is confirmed in `models.py`: `sections.notebook_id` and `pages.section_id` are both `ondelete="CASCADE"` (lines 106, 117). So one empty `get_notebooks()` or `get_sections()` cascades straight down to pages.

The page-level deletes commit immediately (`sync_service.py:393, 409`), so the rows stay gone even when the surrounding job later OOMs or fails.

---

## What the Microsoft Graph docs actually say

The intuition "OneNote list responses are all-or-nothing" is **wrong per Microsoft's own documentation.**

1. **Partial results are a documented behavior — and can arrive WITHOUT a 429.** From the throttling guidance: *"your application may not receive a 429 response from Microsoft Graph API, but may still experience signs of throttling, such as increased latency, **partial results**, or service errors... the service is under high load or degraded health, and is applying adaptive throttling."* There is **no response flag** that says "this list is partial." ([throttling guidance](https://learn.microsoft.com/en-us/graph/throttling))

2. **Large collections can be truncated intentionally.** Once a result set crosses an internal threshold, Graph may stop returning further pages for service stability.

3. **Completeness is signaled only by exhausting `@odata.nextLink`.** A list is complete when you have followed `nextLink` until it is absent. ([paging](https://learn.microsoft.com/en-us/graph/paging))

4. **OneNote paging specifics** ([onenote-get-content](https://learn.microsoft.com/en-us/graph/onenote-get-content)):
   - Default page size is **20**; maximum with `top` is **100**.
   - *"GET requests for pages that retrieve the default number of entries (that is, they don't specify a **top** expression) return an `@odata.nextLink`..."* — i.e. the documented auto-paging path is the **no-`top`** path. OneNote guidance is to page large `top` result sets explicitly with **`skip`** (`?top=50&skip=50`, `?top=50&skip=100`, ...).

### Latent truncation bug in our pagination

`get_notebooks/get_sections/get_pages` all request `?$top=100` (`graph_client.py:640-679`) and rely on `_get_all` following `@odata.nextLink`. Because the documented auto-`nextLink` behavior is tied to the **no-`top`** path, a section/notebook with **more than 100 children** can return exactly 100 with **no `nextLink`**, and `_get_all` stops — silently truncating to 100. The delete-stale logic then deletes children **101+** as "removed from Graph."

This is a **deterministic** data-loss path (any collection >100), independent of the transient throttling one. We currently have no notebook that large, but it is a landmine.

---

## How to detect a degraded / incomplete response

There is no single "partial" flag, but the collection size is checkable:

- **`$count=true` → `@odata.count`.** OneNote supports the `count` query option, which returns the server's authoritative total for the collection in `@odata.count`. **After paging, compare the number of items actually collected against `@odata.count`. If `collected < @odata.count`, the response is incomplete — do NOT run delete-stale.** ([onenote-get-content, `count` option](https://learn.microsoft.com/en-us/graph/onenote-get-content))
- **Sanity floor.** Independently of `$count`, never let a list that has shrunk to empty (or drastically smaller than the current local child count) drive deletions. A parent that had N children and now reports 0 is far more likely a degraded response than a true mass-delete.
- **Delta query (proper long-term answer, beta caveat).** Microsoft's recommended reliable change-tracking is `delta` + `@odata.deltaLink`, which returns explicit add/update/**delete** signals, so deletions are never *inferred by absence*. For OneNote, `note: delta` exists **only in the beta API** (`graph-rest-beta`), not v1.0 — and Microsoft advises against beta in production. Track as the strategic fix, not the immediate one. ([note: delta (beta)](https://learn.microsoft.com/en-us/graph/api/note-delta?view=graph-rest-beta), [delta query overview](https://learn.microsoft.com/en-us/graph/delta-query-overview))

---

## Fix

### Patch A — Guard delete-stale against incomplete lists (minimal, ship first)

The core principle: **only an enumeration we are confident is complete may authorize deletions.** Absence in a possibly-partial list must never hard-delete.

1. Make `_get_all` report completeness, not just items. Request `$count=true`, capture `@odata.count` on the first page, and after paging return both the items and a `complete: bool` (`len(items) == expected_count`, and pagination terminated by an absent `nextLink` rather than an error).
2. In all three reconciliation sites, **skip the delete-stale step when the list is not provably complete.** Upserts (additions/updates) are always safe and should still run; only the deletions are gated. Log a warning when deletions are skipped so it is visible.
3. Add an explicit **empty-list floor**: if Graph returns zero children for a parent that currently has > 0 local children, never delete — log and bail on the delete only.

### Patch B — Fix the pagination so completeness is real

Pick one:

- **Drop `$top`** and let OneNote auto-page at 20 via `@odata.nextLink` (the documented complete-enumeration path). Simpler; more requests.
- **Keep `$top=100` and page with `$skip`** (`skip += 100` until a page returns `< 100` items). Fewer requests; must be paired with the `$count` cross-check from Patch A to be trustworthy.

Either way, completeness must be derived correctly — today it is not.

### Patch C — Prefer soft-delete / tombstones (optional, recoverability)

Hard delete + cascade makes any bad reconciliation unrecoverable. A `deleted_at` tombstone (and deferring physical delete until an item has been absent across N consecutive *complete* syncs) turns a single bad response into a no-op instead of data loss. Larger change; worth it given the blast radius.

### Patch D — Delta query (strategic)

When OneNote `delta` graduates to v1.0 (or if the beta risk is acceptable), move reconciliation to `delta` + `deltaLink` so deletions are explicit and never inferred.

---

## Recommended Order

1. **Patch A** (guard delete-stale on completeness + empty-list floor) — stops the bleeding, small, safe.
2. **Patch B** (correct pagination) — removes the deterministic >100 truncation landmine.
3. **Patch C** (tombstones) — defense in depth / recoverability.
4. **Patch D** (delta) — strategic, gated on v1.0 support.

Patches A and B together close both the transient (degraded `200`) and deterministic (>100 truncation) data-loss paths and are the immediate priority.

---

## Tests

- `_get_all` returns `complete=False` when collected count < `@odata.count`.
- `_get_all` returns `complete=False` when a section has > 100 pages (paging must continue, not stop at 100).
- Reconciliation **does not** delete local pages when the page list is incomplete.
- Reconciliation **does not** delete all pages when `get_pages()` returns `[]` for a section that has local pages.
- Reconciliation **still** upserts additions/updates when the list is incomplete (only deletes are gated).
- A genuine deletion (item absent from a *complete* list) is still applied.

---

## Relationship to the stopgap plan

Same operating conditions (throttling, restarts) trigger both, which is why they showed up together — but they are different bugs. The stopgap plan keeps the worker alive and quota-safe; **this plan keeps it from destroying data.** Land Patch A here before re-enabling aggressive syncing from the stopgap plan, so a throttled run cannot delete pages on the way back up.
