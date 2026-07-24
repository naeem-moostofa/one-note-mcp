# MCP Toolset Redesign

Status: **implemented**, verified against a local Postgres corpus. Not yet exercised in prod.

Goal: 3 tools → 2. Search returns many short anchored snippets; the model expands only the ones it
cares about. Self-contained — everything needed to build this is in this doc.

## Toolset

- `onenote_search(query, notebook=None, section=None)`
  - No `notebook_ids` — searches everything in scope by default.
  - Filters take **names, not integer IDs** (`notebook="CS241"`), case-insensitive.
  - **No verbosity knobs.** Always short snippets; expansion is the detail knob. Two ways to get more context would just invite the model to pick the expensive one.
  - **Flat top ~30 snippets of ~200 chars each — no page grouping.** Ranked best-first across the whole corpus. Snippets are sized to *judge relevance*, not to answer from.
  - **Soft per-page cap 10** so one keyword-dense page can't take all 30 slots.
  - Server-side hard response budget **~9K chars** regardless of args. Arithmetic: 30×200 text + 30×24 handles + 30×60 breadcrumb + ~600 JSON ≈ 9.1K.
- `onenote_expand_snippets(snippet_ids: list[str])`
  - Takes **multiple ids per call** — one round trip instead of N. **No knobs.**
  - **Fixed step** per expansion (~750 chars each side), snapped to enclosing block boundaries (double-newline). Not caller-tunable.
  - **Chainable, no limit**: each expanded snippet returns a *new* `snippet_id` covering the wider region, so it can be expanded again as many times as the model wants.
  - Merge/dedupe overlapping expansions from the same page (`_merge_windows` already does this) — two adjacent snippets expanded in one batch can collide.
  - Returns `list[SearchSnippet]` — same flat model as search. **Partial failure is per-snippet**: a stale handle comes back with `error` set and `text` null; the rest of the batch still returns.
- `onenote_list_notebooks` — **deleted** (keep as alias one release if other clients exist).
- `onenote_get_page` — **deleted.** "Summarize this whole page/section" goes through a section-filtered search.

## Snippet ranking (pulled in — flat top-30 does not work without it)

Today windows are only ordered *within* a page, longest-first, so the biggest merged blob wins.
Flat global ranking needs a real per-snippet score:

1. **Stopword-filter window terms.** Note: FTS *already* handles stopwords — `search_vector` is `to_tsvector('english', …)` and the query goes through `websearch_to_tsquery('english', …)`. **Snippet building bypasses that path entirely**: `_tokenize_terms` (`search_service.py:183-187`) is a raw `\w+` regex, and `_collect_windows` runs `.find()` against the raw `content` column, which is *not* stopword-stripped (`SanitizedText`, `models.py:33-42`, only removes Postgres-unsafe chars). So every `to` in a 40K page spawns a window, and those junk windows compete for the 30 slots. Add a small built-in English stopword set here; if filtering empties the list, fall back to unfiltered.
2. **Score each window** by distinct query terms covered (desc), then match density (desc). Replaces longest-first selection.
3. **Hard cap each snippet at ~200 chars**, split on whitespace — no unbounded merged blobs.
4. **Flatten, sort globally by score, apply the per-page cap of 10, truncate to budget.**

## Tool description steering

The scan-then-expand loop only happens if the descriptions say so. Key lines to land:

- `onenote_search`: *"Returns many short snippets across matching pages. Snippets are deliberately short — long enough to judge which are relevant, not to answer from. Pick the relevant ones and call `onenote_expand_snippets` on their `snippet_id`s."*
- `onenote_expand_snippets`: *"Expand the snippets you judged relevant. Returns them with more surrounding context and a fresh `snippet_id` each — expand again if still not enough. Expand several at once in one call."*
- **Anti-pattern to name explicitly**: *"Do not re-search to get more detail on a page you already found — expand instead. One search is usually enough."*
- Drop every trace of the old "increase `search_size` / `max_pages` / make several alternate queries" guidance from the descriptions and `FastMCP(instructions=...)`.

## Response schemas

All in `app/schemas.py`. **No DB migration — every change is a Pydantic response model.**
One flat model serves both tools. No envelopes, no page nesting.

```python
# CHANGED: flat + self-contained. Gains snippet_id, page breadcrumb, error, stale.
class SearchSnippet(BaseModel):
    snippet_id: str                # NEW — base64("page_id:start:end:crc32"), opaque
    page: str                      # NEW — "CS241(1) › Week 5 › Module 5 Part 1"
    text: Optional[str] = None     # CHANGED — None when error is set
    error: Optional[str] = None    # NEW — "Page X has changed since your last search…"
    stale: bool = False            # NEW — serialize ONLY when true (exclude_defaults)
```

- Both tools return `list[SearchSnippet]`.
- **DELETED: `SearchHit`** (`schemas.py:295-302`) — page grouping goes away entirely.
- **DELETED:** `PageContent` (`schemas.py:336-342`) and `NotebookSummary` (`schemas.py:330-333`) — drop out with their tools.
- Each snippet is **self-contained**: one `page` breadcrumb instead of `page_id` + `page_title` + `section_name` + `notebook_name`. ~60 chars vs ~120, and reads better than an integer id. `page_id` is dropped — `snippet_id` already encodes it.
- Expanded snippets return a **new `snippet_id`** covering the wider region, so they can be expanded again, indefinitely.
- A stale handle still decodes to a `page_id`, so it comes back as a normal snippet with `error` set and `text` null.
- `error` is a separate field rather than error text stuffed into `text`, so the model can't mistake "page changed, re-search" for actual note content and repeat it to the user as if it came from their notes.
- `stale` is **omitted from the response when false** — repeating `stale: false` 30× is pure overhead, but a mid-sync page can return half-synced content and presenting that as complete is a silent wrong answer. Zero bytes in the normal case.
- Unchanged: `Page` model and all DB tables (`models.py:151-175`).

## Also doing

- `@mcp.tool(output_schema=None)` on every tool — FastMCP currently emits every result twice (`content` *and* `structured_content`); this drops the duplicate.
- Skip the trigram fallback when FTS already returned enough hits (`search_service.py:122-129` runs it whenever *any* term is missing — the dominant latency cost).
- Cap the fallback to the ~3 most distinctive terms; exclude numerics and short identifiers.
- Stop selecting `Page.content` in the ranking queries (`page_repository.py:125`, `:188`) — both passes pull full content for every candidate, then discard all but the survivors.

## Implementation notes

**Nothing is stored server-side. No snippets table, no cache.** `snippet_id` is not a key into
anything — it *is* the coordinates. Standard pattern: opaque/stateless cursor (MCP's own
`nextCursor`, DynamoDB `LastEvaluatedKey`, JWTs, S3 presigned URLs).

- Search already computes `_Window(start, end)` and discards it at `search_service.py:270-273`. Emit it instead. `SearchSnippet` (`schemas.py:290-292`) is just `text: str` — no DB migration, all response-model work.
- Handle = base64 of `page_id:start:end:crc32`. Base64 not plaintext, so `27:1420:1900` doesn't visibly invite the model to page-walk by guessing `27:1900:2400`.
- Expand = decode → the same primary-key read `get_page` does today → widen to block boundaries → slice. Source of truth is `pages.content`, which already exists.
- ⚠️ **Scope check on decode is mandatory.** The handle is model-supplied input: decode *then* verify `notebook_id in scope.allowed_notebook_ids` exactly as `tools.py:121` does, or a fabricated handle reads outside the connection scope.
- Handles cost ~24 chars each (~720 chars for 30 snippets) — accounted for in the budget above.

### Staleness hash — decided: `zlib.crc32`, computed at runtime, not stored

- ~28 µs on a 41K-char page (md5 95 µs, blake2b 363 µs). Content is already in memory at search time, so no extra I/O.
- Storing it would need a migration, a new column, and a sync-path change to keep correct — all to save microseconds. Not worth it.
- **crc32 is deliberate, do not "upgrade" it to sha256.** This is a *freshness* check, not a security boundary — the security boundary is the scope check on decode. Forging a hash only ever returns content the caller may already read.
- On mismatch, raise `ToolError` (consistent with `tools.py:120,123`):
  > Page "Module 5 Part 1" has changed since your last search. Call `onenote_search` again to get fresh snippet ids.

**Why not an in-memory KV cache:** it would be a cache that must never miss, i.e. a store. Railway
redeploys wipe it mid-conversation, there is no correct TTL, and multi-instance forces Redis — all
to avoid one indexed PK read plus a string slice.

## Dependencies on other plans

**Pulled into this plan** (hard blockers, listed above): stopword filtering, per-snippet scoring,
per-snippet char cap. Originally Phase 2 of `mcp-context-efficiency.md`.

**Deliberately NOT taken** — none of these block the build, but note the consequence:

| Left out | Consequence |
|---|---|
| Content cleaning (watermark strip, near-dup removal) | Snippets will contain watermark/duplicate OCR noise. Quality hit, not a blocker — and it makes 200-char snippets harder to triage. Strongest candidate to do next. |
| `ts_rank_cd` length normalization | Long keyword-dense pages still win page-level candidate selection, so good pages can miss the candidate set entirely. |
| Title in `search_vector` | Needs a DB migration — explicitly out. "Module 5" won't match the page titled `Module 5 Part 1`. |
| Eval harness | Nothing validates the open questions below. Needed to *tune*, not to ship. |

## Open questions

- Is ~200 chars enough for the model to judge *which* snippet to expand? Load-bearing assumption of the whole design — if snippets are too thin to triage, the model expands blindly or re-searches.
- Does the model actually expand, or try to answer from thin snippets? Watch both failure modes: under-expanding (wrong answers) and expanding everything (context blowup, now unbounded).
- Does deleting `get_page` break anything real?
- Relevance floor to drop junk snippets on vague queries?
- Do notebook/section name filters need fuzzy matching?
- Ever want this usable as a **ChatGPT connector**? That contract needs tools named `search`/`fetch` with `fetch` returning the full record — conflicts with expand-a-snippet, would need a shim.

## Not doing

- A monolithic `ask(question)` RAG tool.
- Embedding/vector search — FTS + trigram is adequate at this corpus size.
- More tools. Three → two is the direction.
