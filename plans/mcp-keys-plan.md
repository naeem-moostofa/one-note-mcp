# Scoped MCP Keys — v1 (Create & Reveal)

Minting a scoped MCP connection key is **the app's headline feature**, so the create
flow lives directly on the notebook dashboard rather than behind a separate page.

Target UX (the user's flow):

> Tick a checkmark on the notebook cards you want (or flip "All notebooks") → a
> "Create key" button appears → press it → a full-screen popup shows the new API key
> and the notebooks it's scoped to → copy the key → start using it.

**v1 scope:** create + reveal only. **No listing, revoking, or deleting yet** — those
come in a later pass (see "Deferred to v2").

## Decisions (locked for v1)

- **Selection model:** per-notebook checkmarks on the dashboard cards, **plus** a
  separate "All notebooks" toggle that mints a key with `scope_all_notebooks = true`
  (and so also covers notebooks added later). The two are mutually exclusive: when
  "All notebooks" is on, the per-card checkmarks are disabled.
- **No key list in v1.** Once the reveal popup is closed, the key is gone from the UI.
  Pure create-and-reveal.
- **Placement:** integrated into the existing dashboard / `NotebookList`. No new route,
  no top-bar nav.

## Current State

**The backend is already done and needs no changes for v1.**

- `mcp_connections` table: `token_hash`, `display_name`, `scope_all_notebooks`,
  `notebook_ids int[]`, `created_at`, `last_used_at`, `revoked_at`.
- `POST /api/mcp-connections` (`MCPConnectionService.create`): mints a token
  (`onmcp_` + 256-bit), stores only its SHA-256 hash, validates that any
  `notebook_ids` are owned by the caller, and returns the raw token **once** plus
  `mcp_url`. This is the only endpoint v1 calls.
- `GET /api/mcp-connections` / `DELETE /api/mcp-connections/{id}` exist already but
  are **not used in v1** (they're for the v2 list/revoke UI).
- MCP auth (`app/mcp/auth.py` + `MCPConnectionService.resolve_token`): on each MCP
  request, hashes the bearer token → looks it up → rejects revoked → **intersects the
  key's scope with the user's currently sync-enabled notebooks** → carries the allowed
  notebook IDs into tool calls. `app/mcp/tools.py` enforces them on every tool.

**The frontend has nothing for this yet.** `frontend/src/features/` has only `auth`,
`account` (just `use-me`), and `notebooks`. The dashboard renders only the notebook
list.

## Why no backend change in v1

The earlier draft added a `scoped_notebooks` (id → display_name) projection to the API
responses. That existed to feed a **key list**, where notebook IDs come back from
storage with no names attached. In v1 there is no list, and the create flow already
knows the names: the user ticks a checkmark on a card that is on screen, so we capture
`{ id, display_name, sync_enabled }` into selection state at that moment. The reveal
popup renders names straight from that state. So `app/schemas.py` /
`mcp_connection_service.py` are untouched in v1; the enrichment moves to v2 with the
list.

## Things Worth Calling Out (the non-obvious bits)

1. **Scope is intersected with *sync-enabled* notebooks at request time.** A key scoped
   to a notebook that isn't sync-enabled (or later gets disabled) silently returns
   nothing for it — only synced notebooks are searchable. The dashboard shows enabled
   *and* disabled notebooks, so a user can tick a disabled one. v1 does **not block**
   that, but flags it: a ticked-but-not-synced notebook shows a subtle "not synced —
   won't return results until you enable sync" hint (on the card and again in the
   reveal popup). We have `sync_enabled` in selection state, so this is free.

2. **The raw token is shown exactly once.** Only the hash is stored; it can never be
   re-displayed. The reveal popup must force a copy with a clear "you won't see this
   again — create a new key if you lose it" warning. (No revoke in v1, so the recovery
   path is "make another key.")

3. **"Start using it" means client config, not just a copied string.** Pair the token
   with `mcp_url` and a ready-to-paste config block per client (Cursor / Claude Code /
   Codex). The token is sent as `Authorization: Bearer <token>` to the MCP endpoint.
   Shown only in the one-time reveal (it embeds the secret).

4. **Selection must survive pagination & filtering.** The notebook list is one
   filtered page at a time (`PAGE_SIZE = 50`, `offset`, `search`, `sync_enabled`,
   `sync_status`). Selection state therefore lives **above** the page data, keyed by
   notebook id, and persists as the user paginates or filters. It's cleared after a
   key is created (popup closed) or via an explicit "Clear selection".

5. **Usefulness preconditions:** the feature only does anything once Microsoft is
   connected *and* at least one notebook is sync-enabled. The existing empty/connect
   states already cover the "no notebooks / not connected" cases.

## Frontend Changes (all of v1's work)

New feature: `frontend/src/features/mcp-keys/` following the established
bulletproof-react layout (`api/` hooks, `components/`, no barrel files, `@/` alias).
Selection state and the create affordance are wired into the existing
`features/notebooks/components/notebook-list.tsx`.

### Types (`src/types/api.ts`)

```ts
export interface MCPConnectionCreated {
  id: number
  display_name: string | null
  scope_all_notebooks: boolean
  notebook_ids: number[] | null
  created_at: string
  raw_token: string
  mcp_url: string
}

export interface CreateMCPConnectionRequest {
  display_name?: string
  scope_all_notebooks: boolean
  notebook_ids?: number[]
}
```

(Skip the `MCPConnection` / `ScopedNotebook` web-list types until v2.)

### Selection state

A small selection store lifted into `NotebookList` (plain `useState<Map<number,
SelectedNotebook>>`, or a dedicated `use-notebook-selection.ts` hook if it grows).

```ts
interface SelectedNotebook { id: number; display_name: string; sync_enabled: boolean }
```

- `allNotebooks: boolean` flag (the "All notebooks" toggle). When true, per-card
  checkmarks are disabled and the create payload is `{ scope_all_notebooks: true }`.
- When false, the payload is `{ scope_all_notebooks: false, notebook_ids: [...] }`.

### API hook (`features/mcp-keys/api/`)

- `use-create-mcp-connection.ts` — `POST /api/mcp-connections` (TanStack Query
  mutation). Returns the one-time `raw_token` + `mcp_url` to the caller for the reveal
  step. Do **not** stash the token in the query cache. No list to invalidate in v1.

### Components

- **Card checkmark** — extend `features/notebooks/components/notebook-card.tsx`: add a
  selection checkbox (distinct from the existing Sync button and auto-sync Toggle).
  New props: `selected`, `onSelectChange`, `selectionDisabled` (true when "All
  notebooks" is on). Show the "not synced" hint when a disabled notebook is ticked.
- **Create bar** (`features/mcp-keys/components/create-key-bar.tsx`) — a sticky bar
  that appears once a selection exists *or* "All notebooks" is on:
  - The "All notebooks" toggle.
  - Optional name field (`display_name`, optional — defaults to none).
  - "Create key" button labelled with the count ("Create key · 3 notebooks" /
    "Create key · all notebooks"), disabled until ≥1 selected or all-notebooks on.
  - On click → calls the create hook → opens the reveal popup with the result.
- **Reveal popup** (`features/mcp-keys/components/key-reveal-modal.tsx`) — full-screen
  overlay (`fixed inset-0`):
  - The raw token in a read-only field + **Copy** button, with the one-time warning.
  - The scope it was granted: "All notebooks", or the list of selected notebook names
    (from selection state), each flagged if not sync-enabled.
  - **Client setup** tabs/snippets for Cursor / Claude Code / Codex (see below).
  - **Done** action → closes, drops the token from memory, clears the selection.

### Client config snippets (the "start using it" payload)

Copy-paste blocks parameterized with `mcp_url` and the raw token:

- **Cursor / Claude Code** (`mcp.json`-style):
  ```json
  {
    "mcpServers": {
      "onenote": {
        "url": "<mcp_url>",
        "headers": { "Authorization": "Bearer <raw_token>" }
      }
    }
  }
  ```
- **Codex**: the equivalent MCP server entry with the same URL + bearer header.

(Exact format finalized against each client's current docs during implementation.)

## Testing Plan

### Backend

No backend change in v1, so no new backend tests. `scripts/smoke_rest.py` and
`scripts/smoke_mcp.py` already cover create/validation and the resolve path
(scope ∩ sync-enabled, out-of-scope filtered).

### Frontend

- `tsc --noEmit`, `eslint`, `vite build` clean.
- Manual:
  - Tick several notebooks across more than one page / a filter change → confirm the
    selection persists and the count is right.
  - Flip "All notebooks" → per-card checkmarks disable; create mints
    `scope_all_notebooks: true`.
  - Create → token appears in the popup; Copy works; closing the popup clears the
    token and the selection.
  - Tick a non-sync-enabled notebook → "not synced" hint shows on the card and in the
    popup.
  - (Optional, real token) paste the config into a client and confirm a scoped search
    works; out-of-scope notebooks return nothing.

## Deferred to v2

- **Listing keys**: `GET /api/mcp-connections` + a key-card list (name, scope by name,
  `created_at`, `last_used_at`). This is where the backend `scoped_notebooks`
  name-projection comes back (the list has IDs but no names).
- **Revoke / delete**: `DELETE /api/mcp-connections/{id}`, confirm-first, show
  `last_used_at` so the user can tell which keys are live.
- Possibly a dedicated `/keys` route if the dashboard gets crowded.
