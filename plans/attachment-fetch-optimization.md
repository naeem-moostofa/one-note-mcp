# Attachment fetch optimization (PDF printouts)

Status: **implemented + verified live** (CS241(1) sync, 2026-06)
Related: `plans/per-user-rate-limit-rework.md`, `plans/sync-rate-limit-fix-plan.md`

**Landed:** `<object>` parsing + `data-options="printout"` skip (`graph_client.py`), the
`utils/pdf_extract.py` extractor/detector, the sync integration (`sync_service.py`), the config
knobs (`config.py`), the `pymupdf` dependency, and the `content_hash` drop (migration
`d2e4f6a8c0b1`, applied). The Graph→1-per-PDF drop confirmed live.

**Live-sync follow-ups (2026-06):** (1) the image-coverage OCR clause was removed — it misfired on
watermarked decks and forced 100% OCR; detection is now text-length only (see §4). (2) MuPDF's
C-core stderr diagnostics (the benign "No common ancestor in structure tree" flood) are routed into
Python logging under the `pymupdf` logger at DEBUG via `fitz.set_messages(pylogging=True)`.

## TL;DR

OneNote "file printouts" of a PDF rasterize **every PDF page into a separate embedded image**, and
we currently fetch each one as its own Graph `$value` request. One STAT 231 lecture page costs
**139 Graph requests** today — over a third of the ~400/hr per-user budget for a *single* OneNote
page. But OneNote also keeps the **original PDF as one attachment resource** (`<object>`), so we
can fetch the whole file in **one** request and pull text locally with PyMuPDF, falling back to
Vision OCR only on the figure-heavy pages. Net for that page: **139 Graph requests → 1**, with
*better* OCR quality than today.

## Evidence (from the throwaway probes)

Two probe scripts under `backend/scripts/` established the facts (they download via the real
`GraphClient`, so the rate limiter applies; outputs cached under `scripts/_probe_out/`):

- `probe_page_attachments.py` — confirms printout pages carry `<object data-attachment="*.pdf"
  type="application/pdf" data="…/resources/{id}/$value">` alongside the per-page `<img>`s, and that
  every printout image is tagged `data-options="printout"` (+ `data-index` = 1-based PDF page number,
  + a unique `data-id`) while loose pasted images carry none of these. Run dumps the raw element
  attrs to `_probe_out/elements_*.json` for inspection.
- `analyze_pdf_extraction.py` — downloads one PDF once and compares PyMuPDF text vs a rendered
  image vs Vision OCR per page; also measures embedded-image area coverage per page.

Measured on `STAT231(1) / Chapter 1 Slides.pdf` (138 pages, 9.4 MB, currently 139 image fetches):

| Bucket | Pages | Handling |
|---|---|---|
| Pure text (0% image coverage) | 121 | PyMuPDF text only — no OCR |
| Low-text figures (<50 chars, 60–90% coverage) | 11 | render page locally → Vision |
| Text + figure (enough text but >35% coverage) | 6 (p51,73,76,77,78,91) | PyMuPDF text **and** OCR the render |
| **OCR needed** | **17 / 138** | the other 121 are free local text |

Across both notebooks tested, **7/7 sampled PDFs were text-bearing** (AFM274(1): 6 decks, 0 empty
pages; STAT231(1): the 138-page deck). So PyMuPDF is the primary path and Vision is the fallback.

## Quality boundaries (document these as expected behavior, not bugs)

The extracted text is aimed at **search / retrieval / Q&A in the MCP**, not faithful reproduction.
Verified quality, by content type:

- **Prose / bullets / captions** — *lossless* via PyMuPDF (verbatim, correct order). This is the
  bulk (121/138 pages here).
- **Titles, axis labels, legends, annotations on charts** — *good* via Vision, but returned as a
  **non-linear, jumbled blob** with occasional small-text misreads.
- **Math / formulas** — *partial*. PyMuPDF **garbles** math layout: the formula `ȳ=159.77,
  s²=36.36, s=6.0` on p51 came out as scrambled fragments (`0.6 , 36 . 36`). Vision reading the
  *rendered* page does better, so pages that also get OCR'd (any "text+figure" page) recover their
  math; pure-formula slides with no figure keep the garbled PyMuPDF version.
- **Chart data / trend / shape** — *lost by both tools.* No OCR reconstructs bar heights or a
  distribution shape from a rasterized chart; you get the labels, not the numbers.

Why this is acceptable for the use case: the *conceptual takeaway* of each chart is almost always
written on the adjacent text slide (the temp-histogram on p55 is followed by p56 "mean 13.12,
median 14.2, skewed left"; the precipitation chart p57 by p58 "long tail to the right"). That prose
is captured losslessly, so an agent gets the idea — from the explanation slide, not the bars.

This approach is also **strictly better than today's pipeline** for these decks: today we composite
all 139 slide images into one oversized canvas that must be downscaled to fit Vision's 75 MP cap,
blurring small axis text. Per-page rendering at native resolution OCRs better.

## Design

### 1. Parse `<object>` attachments

Extend `_parse_page_elements` (`backend/app/clients/graph_client.py:250`) to recognize
`<object>` elements with `data-attachment` + `type="application/pdf"` + a `data` URL, emitting a new
element kind (e.g. `GraphPageElement(kind="pdf_attachment", attachment_name=…, resource_url=…)`).
The `data` URL is the resource `$value` we fetch once. (Confirmed on a real deck: the object carries
only `data-attachment` / `type` / `data` — no id of its own — so we do **not** need to correlate
images back to the object; see §2.)

### 2. Skip the printout images (`data-options="printout"`)

**Confirmed mechanism (STAT231 "Chapter 1 Slides", 138-page deck):** OneNote tags every rasterized
PDF-page image with `data-options="printout"` (plus `data-index` = the 1-based PDF page number, and
a unique `data-id`). Genuine pasted images carry **none** of these. So:

- `<img>` with `data-options="printout"` → a PDF page raster → **skip it** (covered by the single
  `<object>` `$value` fetch in §3).
- `<img>` without `data-options="printout"` → genuine loose image → **fetch + process individually**,
  exactly as today (`kind="image"`).

This is where the 139→1 reduction comes from, and it is *per-image*, so it handles **mixed pages**
correctly: the Slides page had 138 printout rasters **and** 1 pasted banner image with no
`data-options`; we skip exactly the 138 and keep the banner as an individual image. No
`data-id`↔object correlation, and no "skip all images on a page with a PDF object" heuristic (which
would have wrongly dropped that banner). Loose-image-only pages (STAT231 "Quiz 1 Formulas" /
"Practice Problems") have no `<object>` and no `printout` images, so every image is fetched as today.

### 3. Fetch the PDF once + extract per page

- Fetch the `<object>` `data` URL via the existing `get_page_image` path (one `$value` request).
- Open with PyMuPDF (`fitz`) from the in-memory bytes.
- For each PDF page: `page.get_text("text")` (free embedded text) + decide if OCR is also needed
  (detector below). When OCR is needed, render *that page* with `page.get_pixmap(matrix=…)` — a
  **local** render, no extra Graph call — and run it through the existing `OCRClient.run_ocr_async`.
- Merge: keep PyMuPDF text always; append Vision text for OCR'd pages (dedupe lines already present
  in the embedded text to avoid doubling).

### 4. The OCR detector (text-length only)

A PDF page is sent to OCR **only when its embedded text is shorter than `OCR_TEXT_THRESHOLD`**
(~50 chars) — i.e. a figure/scan page with no usable text layer. Otherwise the embedded text is
trusted as-is (free, lossless).

**Postmortem — the image-coverage clause was removed (2026-06).** The original design added a second
signal: OCR a page if image-area coverage (`sum of get_image_rects areas / page area`) exceeded
~0.35, to catch "text + figure" pages. Live testing on CS241(1) showed it does more harm than good:

- The coverage sum **double-counts overlapping image rects**, so it isn't a real coverage fraction.
  CS241 slide decks stamp ~8 small images per page (a per-page username watermark); each covered
  ~40%, summing to **323%** on *every* page.
- That tripped the clause on text-rich slides, forcing OCR on **61/61 pages** of a deck whose
  embedded text was perfectly good — replacing clean text with noisy OCR (watermark + misreads) and
  burning ~500–600 Vision calls per notebook sequentially.

The clause it was meant to serve ("enough text to pass the char threshold, but a figure carries
extra un-extractable labels") is a real but rarer case, and the user accepted deferring it
(consistent with "garbled math acceptable for now"). If it ever matters, re-add a **better** signal
(e.g. union-area coverage capped at 1.0, gated behind a low embedded-text bar) rather than the
overlap-summing version. `_image_coverage` was deleted with the clause.

### 5. Incremental sync interaction

The existing per-page incremental skip (`graph_page.last_modified_datetime > last_synced_at`,
`sync_service.py:388`) already means a notebook re-sync doesn't re-fetch unchanged pages, so the one
PDF fetch is a **one-time cost per page**. That is the whole mechanism — Graph's page-level
`lastModifiedDateTime` is the source of truth for "changed," and we lean on it directly.

We do **not** content-hash the PDF resource: to hash it you must first download it, which already
spends the rate-limited Graph `$value` request the hash was meant to avoid. It could only save local
PyMuPDF/OCR CPU after the fact, which is not the bottleneck. (The only thing that could cheaply skip
the *download* on a page that was touched but whose PDF is unchanged is a resource-level ETag from
Graph — not a hash we compute. Out of scope unless that turns out to be both available and needed.)

### 6. Drop the unused `pages.content_hash` column

`pages.content_hash` (`models.py:122`) is written on every sync (`_compute_hash(content)`,
`sync_service.py:481`) but **never read for any decision** — no change-detection, no re-embedding
gate, no dedup. Its only consumer is `get_with_context` (`page_repository.py:49`), which projects it
into `PageDetailResponse` / the MCP `onenote_get_page` output, where it's an opaque string nothing
branches on. It was presumably intended as a "skip re-processing identical content" signal, but the
page-level `last_modified_datetime` skip (§5) already prevents re-processing unchanged pages, so the
hash is redundant — the same reasoning that kills the PDF content-hash above. Remove it alongside
this work:

- **Migration:** drop the `content_hash` column from `pages` (new Alembic revision; the column is
  nullable with no index/constraint — `c51d7c14c289:85` — so the drop is trivial).
- **Model:** remove the `content_hash` column from `Page` (`models.py:122`).
- **Sync writes:** drop `content_hash=…` at `sync_service.py:481` and `:533`, and the now-unused
  `_compute_hash` helper if nothing else references it.
- **Schemas:** remove the `content_hash` field from the page schemas that carry it (`schemas.py:74`,
  `:232`, `:306`, `:437`) and the repo `select` projection (`page_repository.py:49`).
- **Test/seed scripts:** drop the `content_hash` keys from `bulk_seed_pages.py`, `smoke_mcp.py`,
  `verify_search_service.py`.

This removes a field from the MCP `onenote_get_page` response shape — acceptable since no client
logic depends on it; just confirm no consumer reads it before landing.

### Per-OneNote-page request math

- **Today:** N image `$value` fetches (N = PDF page count) + composite + 1 Vision call.
- **Proposed:** 1 PDF `$value` fetch + 0 extra Graph + (pages-needing-OCR) Vision calls, all on
  locally rendered images. Graph drops from N→1; Vision moves off a giant downscaled composite onto
  native-resolution per-page renders (more calls, but Vision is a separate generous quota, not the
  400/hr Graph bottleneck).

## Config knobs (new)

- `SYNC_PDF_OCR_TEXT_THRESHOLD: int = 50`
- `SYNC_PDF_RENDER_DPI: int = 150` (render scale for OCR'd pages)

(`SYNC_PDF_OCR_COVERAGE_THRESHOLD` was added then removed with the coverage clause — see §4.)

## Dependencies

- Add **`pymupdf`** to `backend/pyproject.toml` (used ephemerally via `uv run --with pymupdf` during
  probing; make it a real dependency for the feature). Note Windows-on-ARM wheel availability
  (see `[[windows-on-arm-native-toolchain]]`); PyMuPDF ships arm64 wheels but verify install.

## Testing

- Unit: `_parse_page_elements` emits a `pdf_attachment` element and suppresses the
  `data-options="printout"` images, while keeping loose images on the **same** page (fixture HTML
  modelled on the real Slides page: 1 `<object>` + N `printout` imgs + 1 loose img).
- Unit: the OCR detector — pure-text page (no OCR), figure page (OCR via low text), text+figure page
  (OCR via coverage). Use small synthetic PDFs.
- Integration: a stubbed page with a PDF object fetches the object **once** and makes no per-image
  `$value` calls (assert on the mocked transport).
- Keep `analyze_pdf_extraction.py` as a manual QA harness (not CI) for eyeballing new decks.

## Rollout

1. Land parse + skip + fetch-once + PyMuPDF text (no OCR). Verify the 139→1 drop on a real notebook
   and that prose text is captured.
2. Add the dual-signal detector + per-page Vision fallback. Start **sequential** within a PDF (the
   `for each PDF page → run_ocr_async` loop in §3) — simplest, and lets us measure real per-page sync
   timing before adding concurrency.
3. Drop the unused `pages.content_hash` column (§6) — independent of the above, can land in any
   order; a self-contained migration + model/schema/sync cleanup.

## Future optimizations (measure first, add only if needed)

**Parallel intra-PDF OCR fan-out.** §3 OCRs a PDF's figure pages one at a time. A figure-heavy deck
can have ~17 OCR'd pages on a single OneNote page, so the sequential Vision calls dominate that
page's sync latency. Since OCR runs on the **separate Vision quota** (not the 400/hr Graph budget),
fanning these out is cheap and carries no Graph-limit risk. Only worth doing if step 2's measured
timing shows the sequential OCR is actually the bottleneck. If added, the shape is:

- Keep all PyMuPDF work (`get_text` + `get_pixmap` renders) in **one** `asyncio.to_thread` per PDF —
  a `fitz.Document` is not thread-safe, so never parallelize across pages of the same document. That
  thread emits the full text + a list of `(page_index, png_bytes)` to OCR.
- Back on the event loop, `asyncio.gather` the renders through `OCRClient.run_ocr_async`. **No new
  limiter** — `run_ocr_async` already self-throttles on the process-wide `SYNC_VISION_CONCURRENCY`
  semaphore (`ocr_client.py:26`), so the within-PDF fan-out and the existing across-page workers all
  share one global Vision cap and total concurrent OCR can't exceed the quota.
- Merge in page order (same as §3).

## Open questions

1. ~~Exact printout↔object linkage~~ **Resolved:** printout images are tagged
   `data-options="printout"` (per-image, no object correlation needed). Confirmed on STAT231; worth a
   second glance if a future deck's printout images ever lack the tag, but the marker is explicit.
2. Pure-formula slides (no figure) keep PyMuPDF's garbled math. **Can't be auto-detected by a
   confidence/quality signal** — PyMuPDF does no recognition, so there is no confidence value, and
   the garble is a *reading-order* scramble of valid glyphs, not an encoding failure: measured on
   p51, the garbled-math slide extracts 92 chars with **0** replacement (U+FFFD) chars and fonts
   present, i.e. it looks like a clean success to every extraction-side signal (char count,
   replacement ratio, font presence). So the text-length detector (see §4 — image coverage was tried
   and removed) is already as good as extraction signals get; catching garbled math would need a non-confidence
   heuristic ("many math fonts → force OCR") or OCR-of-render-and-compare. Lean: **accept for now**
   for the search use case, revisit only if math retrieval proves to matter.
3. ~~Non-PDF attachments (Word/PowerPoint printouts)~~ **Decided: out of scope.** Not supporting
   non-PDF printouts for now. A non-PDF `<object>` simply won't match the `type="application/pdf"`
   check (§1), so its printout images fall through to the existing per-image path — no special
   handling, no regression. Revisit only if such notebooks show up and the per-image cost matters.
4. ~~Duplicate notebooks (`STAT231` vs `Stat231(1)`)~~ **Not a duplicate.** Confirmed with the user:
   these are genuinely separate notebooks despite the similar names; both are kept and synced as-is.
   No action.

## Cleanup

The probe scripts (`backend/scripts/probe_page_attachments.py`,
`backend/scripts/analyze_pdf_extraction.py`) and their output dir (`backend/scripts/_probe_out/`)
are throwaway investigation tools. Gitignore `scripts/_probe_out/` and decide whether to keep the
probes as QA harnesses or delete them once the feature lands.
