# Always-beta page fetch (collapse the double content call)

Status: **planned** (no code changes yet)
Related: `plans/attachment-fetch-optimization.md`, `plans/per-user-rate-limit-rework.md`

## TL;DR

Every handwriting page is currently fetched **twice**: once from the v1.0 `/content` endpoint for
HTML, then again from the beta `/content?includeInkML=true` endpoint for the ink. But the beta
response is multipart and **already contains the same HTML** plus the InkML. Switch
`get_page_content_with_ink` to make **one beta call per page** and pull both parts out of it, falling
back to the v1.0 `/content` call **only if the beta request fails** (HTML only, no ink). Saves one
Graph call per ink page with no quality change.

## Evidence (live probes, 2026-06, CS241(1))

- Beta `…/pages/{id}/content?includeInkML=true` returns `multipart/mixed` with parts
  `['text/html', 'application/inkml+xml']` — confirmed on both an ink page (Tutorial 1) and a
  non-ink page (About the Prof). The inkml part is present even when the page has no strokes.
- The beta `text/html` part is **byte-identical** to the v1.0 `/content` HTML on the page tested
  (Tutorial 1: 20916 == 20916 chars; same `<!-- InkNode is not supported -->` markers). So parsing
  text/image/`<object>` elements off the beta HTML yields the same result as today.

## Current code (what changes)

`backend/app/clients/graph_client.py`:

- `get_page_content_with_ink` (≈557) — v1.0 HTML fetch, then conditional `_get_inkml` beta fetch.
  Docstring: *"Makes 1 API call for typed pages, 2 for pages with ink."*
- `_get_inkml` (≈507) — beta fetch that throws away the HTML part and returns only the InkML.
- `get_page_content` (≈548) — plain v1.0 `/content` → text. **Kept** (used by `analyze_pdf_extraction.py`
  and the probe scripts, and now also as the fallback path).

## Design

One beta call returns `(html, inkml_xml)`; the caller parses elements from `html` and strokes from
`inkml_xml`. On any beta failure, fall back to the v1.0 `/content` HTML with no ink.

### 1. New multipart splitter (replaces the inkml-only walk in `_get_inkml`)

```python
def _split_content_multipart(content_type: str, content: bytes) -> tuple[str | None, str | None]:
    """Parse a beta /content?includeInkML=true response into (html, inkml_xml).

    The response is multipart/mixed with a text/html part and an application/inkml+xml part. Either
    may be absent on odd responses (a non-multipart HTML-only body parses as a single text/html
    part), so both are returned as Optional and the caller decides.
    """
    raw = b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + content
    message = email.message_from_bytes(raw)
    html = inkml = None
    for part in message.walk():
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        ctype = part.get_content_type()
        if ctype in ("text/html", "application/xhtml+xml"):
            html = payload.decode("utf-8", errors="replace")
        elif ctype == "application/inkml+xml":
            inkml = payload.decode("utf-8", errors="replace")
    return html, inkml
```

### 2. Beta fetch returning both parts (replaces `_get_inkml`)

```python
async def _get_content_with_inkml(self, access_token: str, page_id: str) -> tuple[str, str | None]:
    """One beta call → (html, inkml_xml). Raises on HTTP failure so the caller can fall back."""
    url = f"{_BETA_URL}/me/onenote/pages/{page_id}/content?includeInkML=true"
    response = await self._get(url, access_token)
    html, inkml = _split_content_multipart(response.headers.get("content-type", ""), response.content)
    if html is None:               # unexpected shape — treat the whole body as HTML
        html = response.text
    return html, inkml
```

Note: unlike today's `_get_inkml`, this does **not** swallow exceptions — the fallback lives in the
caller so a beta failure still yields HTML (via v1.0), not a dropped page.

### 3. Caller: beta-first, v1.0 fallback

```python
async def get_page_content_with_ink(self, access_token: str, page_id: str) -> GraphPageContent:
    """Fetch page content + ink in ONE beta call (its multipart response carries both the HTML and
    the InkML). Falls back to the v1.0 /content endpoint (HTML only, no ink) if the beta call fails,
    so a beta hiccup degrades ink rather than failing the whole page."""
    try:
        html, inkml_xml = await self._get_content_with_inkml(access_token, page_id)
    except Exception as error:
        logger.warning(
            "beta content fetch failed for page %s (%s) — falling back to v1.0 /content (no ink)",
            page_id, error,
        )
        html = await self.get_page_content(access_token, page_id)
        inkml_xml = None

    elements = _parse_page_elements(html)
    has_handwriting = _INK_NODE_COMMENT in html
    ink_strokes = _parse_inkml_strokes(inkml_xml) if inkml_xml else []
    return GraphPageContent(elements=elements, ink_strokes=ink_strokes, has_handwriting=has_handwriting)
```

### 4. Cleanup

- Delete `_get_inkml` (fully replaced).
- `get_page_content` stays (fallback + scripts).
- Update the `get_page_content_with_ink` docstring (now "1 call/page, 2 only on beta-failure fallback").

## Behavior parity check

- `has_handwriting` still comes from the `<!-- InkNode is not supported -->` marker in the HTML —
  unchanged signal, so the composite/OCR path is driven exactly as before.
- Non-ink pages: beta returns an empty/strokeless inkml part → `ink_strokes == []`, same as today.
- Beta-failure pages: `inkml_xml is None` → `ink_strokes == []` while `has_handwriting` may be True;
  `sync_service` already logs *"InkML fetch failed — ink will not appear in composite"* in that case
  (existing behavior preserved).

## Risks / things to verify before/at rollout

1. **HTML parity on image/object pages.** Only verified beta-HTML == v1.0-HTML on an ink page and a
   text page. Before trusting it everywhere, confirm a **picture-heavy page** and a **PDF-`<object>`
   page** also return identical HTML (same `data-fullres-src` / `<object data=…>`), so loose-image
   and PDF-attachment parsing is unaffected. Cheap to check with the existing probe pattern.
2. **Broader beta reliance.** Today beta is hit only for ink pages; this makes *every* page depend on
   beta. The v1.0 fallback covers outright failures, but watch for beta-specific quirks (throttling
   behavior, occasional schema drift). Low risk — beta `includeInkML` has been stable and we already
   depend on it for the bulk of pages.
3. **Response size.** Non-ink pages now also carry an inkml part; observed to be negligible, but note
   it's slightly more bytes per non-ink page (not more calls).

## Cost impact — CS241(1)

The saving is exactly **one Graph call per handwriting page** (the dropped redundant v1.0 fetch).
CS241(1) has 22 pages, ~21 with ink (only *About the Prof* is plain text), 8 sections.

| Component | Now | After |
|---|---|---|
| Section list | 1 | 1 |
| Page lists (8 sections) | 8 | 8 |
| Page content (HTML + ink) | ~43  (21 ink ×2 + 1 ×1) | **22**  (1 per page) |
| PDF source `$value` (~9 decks) | ~9 | ~9 |
| Pasted-image `$value` | ~30–50 (varies by page) | ~30–50 (unchanged) |
| **Total** | **~90–110** | **~70–90** |

**Net: ~21 fewer Graph calls (~20%) on a CS241 full sync**, with no change to extracted content or
ink quality. The image `$value` calls are unchanged and remain the largest variable component (a
separate lever — skipping tiny decorative images — is noted in `attachment-fetch-optimization.md`).
Reminder: this is a **first-sync** figure; incremental re-syncs only touch changed pages.

## Testing

- Unit: `_split_content_multipart` on (a) a real two-part beta body, (b) a single-part HTML body,
  (c) a body with only the html part — assert correct `(html, inkml)` extraction.
- Integration (live, opt-in): one ink page, one text page, one image page, one PDF-object page —
  assert parsed elements match a v1.0 `/content` parse of the same page, and ink strokes still load.
- Force a beta failure (bad URL / injected error) and assert the v1.0 fallback returns HTML with
  `ink_strokes == []`.

## Rollback

Single-file change in `graph_client.py`; revert restores the two-call path. No schema/migration.
