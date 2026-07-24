"""
Page search and snippet expansion.

Retrieval is two-pass:
1. Postgres FTS via `websearch_to_tsquery` — fast, precise, catches everything
   in typed text and well-OCR'd words.
2. Trigram word-similarity fallback — handles OCR garbling like
   `painters` ↔ `pointers`. Only runs when FTS came back thin, since it is by
   far the more expensive pass.

Both passes rank *pages*. Content is then fetched for the survivors, cut into
windows that are scored individually, and the whole set is flattened and sorted
globally so the best passages win regardless of which page they came from.

See `plans/mcp-toolset-redesign.md`.
"""

from __future__ import annotations

import base64
import re
import zlib

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NotebookSyncStatus, PageSyncStatus
from app.repositories.page_repository import PageRepository
from app.schemas import (
    PageFTSHit,
    PageTrgmHit,
    PageWithPath,
    SearchSnippet,
)


# ---- Tunables -------------------------------------------------------------

# Half-width of the window cut around each match. A snippet is therefore roughly
# SNIPPET_HALF_WIDTH * 2 characters — long enough to judge relevance, short
# enough that thirty of them stay cheap.
SNIPPET_HALF_WIDTH = 100

# Ceiling on a snippet once overlapping windows have been absorbed into it. A
# lone window is SNIPPET_HALF_WIDTH * 2 plus the matched term, so this leaves
# room for about one more match before a dense cluster stops growing.
MAX_SNIPPET_CHARS = 400

# Response shape. None of these are caller-tunable: the server owns the budget.
MAX_SNIPPETS = 30
MAX_SNIPPETS_PER_PAGE = 10
RESPONSE_BUDGET_CHARS = 9000

# The handle, plus JSON field names, quotes, separators and escaping, around one
# serialized snippet. Measured at 75-99 across the corpus; rounded up, because
# under-counting here is what lets a response run past RESPONSE_BUDGET_CHARS.
_JSON_OVERHEAD_PER_SNIPPET = 100

# Pages considered for snippet extraction after ranking.
CANDIDATE_PAGES = 20

# The trigram pass is the dominant latency cost, so it only runs when FTS came
# back with fewer pages than this — not merely because some query term was
# missing from the FTS hits.
FTS_HITS_SUFFICIENT = 10

# Trigram word_similarity cutoff for the fuzzy fallback. 0.3 is permissive
# enough to catch OCR drift (`painters` vs `pointers` scores ~0.5) without
# flooding results with noise.
TRGM_THRESHOLD = 0.3

# Weight of trigram similarity relative to FTS rank when combining page scores.
TRGM_RANK_WEIGHT = 0.5

# Terms that are useless as trigram probes — pure digits and very short
# identifiers never fuzzy-match meaningfully, but each one adds a
# word_similarity expression over full page content.
MAX_FALLBACK_TERMS = 3
MIN_FALLBACK_TERM_LENGTH = 4

# Characters added on each side per expansion step.
EXPAND_STEP_CHARS = 750

# Applied to the Python-side window/trigram terms only. Postgres already drops
# stopwords when building `search_vector` and when parsing the query, but
# snippet building bypasses that path entirely: it scans the raw `content`
# column, which is full text. Without this filter every "to" in a 40K-char page
# spawns a window, and those junk windows compete for the thirty slots.
STOPWORDS = frozenset(
    """
    a an and are as at be been but by for from had has have he her his how i if in
    into is it its me my no not of on or our so than that the their them then there
    these they this to was we were what when where which who why will with you your
    """.split()
)


# ---- Internal types -------------------------------------------------------


class _RankedPage(BaseModel):
    """Per-page accumulator across the FTS and trigram passes."""
    page_id: int
    fts_rank: float = 0.0
    trgm_score: float = 0.0

    @property
    def combined_score(self) -> float:
        return self.fts_rank + TRGM_RANK_WEIGHT * self.trgm_score


class _Window(BaseModel):
    """Half-open character window into pages.content."""
    start: int
    end: int


class _ScoredWindow(BaseModel):
    """A window on a page, with its match-quality signals."""
    page_id: int
    start: int
    end: int
    distinct_terms: int
    density: float

    @property
    def sort_key(self) -> tuple[int, float]:
        return (self.distinct_terms, self.density)


# ---- Snippet handles ------------------------------------------------------
#
# A handle *is* the coordinates — page id, character range into `pages.content`,
# and a CRC32 of the content it was minted against — so nothing is stored
# server-side. Base64 rather than plaintext, so `27:1420:1900` doesn't visibly
# invite guessing the next range.
#
# The checksum is a freshness check, not a security boundary: it catches a page
# re-synced underneath a handle so expansion fails loudly instead of returning
# the wrong passage. Scope is enforced separately, on decode. A fast
# non-cryptographic hash is therefore sufficient.


class _DecodedHandle(BaseModel):
    page_id: int
    start: int
    end: int
    content_checksum: str


def _content_checksum(content: str) -> str:
    """CRC32 of page content, as 8 lowercase hex digits."""
    return f"{zlib.crc32(content.encode('utf-8')) & 0xFFFFFFFF:08x}"


def encode_handle(page_id: int, start: int, end: int, content: str) -> str:
    raw = f"{page_id}:{start}:{end}:{_content_checksum(content)}"
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def _decode_handle(handle: str) -> _DecodedHandle | None:
    """Decode a handle, or None if it is malformed. Never raises on bad input."""
    try:
        padding = "=" * (-len(handle) % 4)
        raw = base64.urlsafe_b64decode(handle + padding).decode("ascii")
        page_id, start, end, checksum = raw.split(":")
        decoded = _DecodedHandle(
            page_id=int(page_id),
            start=int(start),
            end=int(end),
            content_checksum=checksum,
        )
    except ValueError:
        # Covers bad base64 (binascii.Error), bad utf-8, and bad ints — all
        # ValueError subclasses.
        return None

    if decoded.start < 0 or decoded.end <= decoded.start:
        return None
    return decoded


# ---- Service --------------------------------------------------------------


class SearchService:
    def __init__(self, session: AsyncSession) -> None:
        self._pages = PageRepository(session)

    async def search(
        self,
        query: str,
        notebook_ids: list[int],
        notebook: str | None = None,
        section: str | None = None,
    ) -> list[SearchSnippet]:
        """
        Search pages within `notebook_ids`.

        `notebook_ids` is the authorisation boundary — it is trusted, not
        re-checked. `notebook` and `section` are optional name filters applied
        on top of it.
        """
        query = (query or "").strip()
        if not query or not notebook_ids:
            return []

        fts_hits = await self._pages.search_fts(
            notebook_ids, query, CANDIDATE_PAGES,
            notebook_name=notebook, section_name=section,
        )

        trgm_hits: list[PageTrgmHit] = []
        if len(fts_hits) < FTS_HITS_SUFFICIENT:
            fallback_terms = _fallback_terms(query)
            if fallback_terms:
                trgm_hits = await self._pages.search_trgm(
                    notebook_ids, fallback_terms, TRGM_THRESHOLD, CANDIDATE_PAGES,
                    notebook_name=notebook, section_name=section,
                )

        if not fts_hits and not trgm_hits:
            return []

        ranked = _merge_ranked(fts_hits, trgm_hits)
        ranked.sort(key=lambda page: page.combined_score, reverse=True)
        top_pages = ranked[:CANDIDATE_PAGES]
        page_ids = [page.page_id for page in top_pages]

        paths = await self._pages.get_pages_with_path(page_ids)
        path_by_id = {path.page_id: path for path in paths}
        # A page with no content has nothing to cut a snippet from; dropping it
        # here means one absent-content check covers it everywhere downstream.
        contents = {path.page_id: path.content for path in paths if path.content}

        terms = _window_terms(query)
        scored: list[_ScoredWindow] = []
        for page in top_pages:
            content = contents.get(page.page_id)
            if content is None:
                continue
            scored.extend(_score_windows(page.page_id, content, terms))

        # Global sort: the best passages win regardless of which page they came
        # from. Page rank only decided which pages were considered at all.
        scored.sort(key=lambda window: window.sort_key, reverse=True)

        return _assemble(scored, contents, path_by_id)

    async def expand_snippets(
        self,
        snippet_ids: list[str],
        allowed_notebook_ids: list[int],
    ) -> list[SearchSnippet]:
        """
        Widen each snippet to `EXPAND_STEP_CHARS` more on either side.

        Results come back in request order, each with a fresh handle for its
        wider range. Failures are per-snippet: a malformed, out-of-scope or
        stale handle returns an `error` snippet without failing the batch.
        """
        if not snippet_ids:
            return []

        decoded = {handle: _decode_handle(handle) for handle in snippet_ids}
        page_ids = list({d.page_id for d in decoded.values() if d is not None})

        paths = await self._pages.get_pages_with_path(page_ids)
        path_by_id = {path.page_id: path for path in paths}
        contents = {path.page_id: path.content for path in paths if path.content}
        allowed = set(allowed_notebook_ids)

        widened: list[tuple[str, _Window, str, PageWithPath]] = []
        failures: dict[str, SearchSnippet] = {}

        for handle in snippet_ids:
            decoded_handle = decoded[handle]
            if decoded_handle is None:
                failures[handle] = _error_snippet(
                    handle, "Malformed snippet_id. Call onenote_search to get valid snippet ids."
                )
                continue

            path = path_by_id.get(decoded_handle.page_id)
            content = contents.get(decoded_handle.page_id)
            # An out-of-scope page is reported the same way as a missing one so
            # the response never confirms that someone else's page exists.
            if path is None or content is None or path.notebook_id not in allowed:
                failures[handle] = _error_snippet(
                    handle, "Snippet not found or outside this connection's scope."
                )
                continue

            if _content_checksum(content) != decoded_handle.content_checksum:
                failures[handle] = _error_snippet(
                    handle,
                    f'Page "{path.page_title or "Untitled"}" has changed since your last search. '
                    "Call onenote_search again to get fresh snippet ids.",
                    path=path,
                )
                continue

            window = _Window(
                start=max(0, decoded_handle.start - EXPAND_STEP_CHARS),
                end=min(len(content), decoded_handle.end + EXPAND_STEP_CHARS),
            )
            widened.append((handle, window, content, path))

        expanded = {
            handle: SearchSnippet(
                snippet_id=encode_handle(path.page_id, window.start, window.end, content),
                page=_breadcrumb(path),
                text=_clean_snippet_text(content[window.start:window.end]),
                stale=_is_stale(path),
            )
            for handle, window, content, path in widened
        }

        # Request order, so results line up with what was asked for.
        return [failures.get(handle) or expanded[handle] for handle in snippet_ids]


# ---- Helpers --------------------------------------------------------------


# Word-ish: letters, digits, underscore. Anything else acts as a separator.
# Strips FTS operators (`&`, `|`, `!`, parens, quotes) along with punctuation
# so the substring + trigram passes don't carry tsquery syntax with them.
_TERM_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize_terms(query: str) -> list[str]:
    """Split a query into bare terms of two characters or more."""
    return [match.group(0) for match in _TERM_RE.finditer(query) if len(match.group(0)) >= 2]


def _window_terms(query: str) -> list[str]:
    """Query terms with stopwords removed."""
    terms = _tokenize_terms(query)
    filtered = [term for term in terms if term.lower() not in STOPWORDS]
    # An all-stopword query would otherwise produce no windows at all.
    return filtered or terms


def _fallback_terms(query: str) -> list[str]:
    """The longest few terms worth probing with trigram similarity.

    Pure digits and short identifiers never fuzzy-match usefully, and each term
    adds a `word_similarity` expression over full page content, so the list is
    kept short.
    """
    candidates = [
        term for term in _window_terms(query)
        if len(term) >= MIN_FALLBACK_TERM_LENGTH and not term.isdigit()
    ]
    candidates.sort(key=len, reverse=True)
    return candidates[:MAX_FALLBACK_TERMS]


def _merge_ranked(
    fts_hits: list[PageFTSHit],
    trgm_hits: list[PageTrgmHit],
) -> list[_RankedPage]:
    """Combine FTS + trigram hits into one ranked-page accumulator per page_id."""
    by_id: dict[int, _RankedPage] = {}

    for hit in fts_hits:
        by_id[hit.page_id] = _RankedPage(page_id=hit.page_id, fts_rank=hit.rank)

    for hit in trgm_hits:
        existing = by_id.get(hit.page_id)
        if existing is None:
            by_id[hit.page_id] = _RankedPage(page_id=hit.page_id, trgm_score=hit.score)
        else:
            existing.trgm_score = max(existing.trgm_score, hit.score)

    return list(by_id.values())


def _score_windows(page_id: int, content: str, terms: list[str]) -> list[_ScoredWindow]:
    """Cut and score one window around every match on a page.

    Windows overlap freely — near-duplicates are resolved during selection,
    once the global ranking has decided which of them the model actually sees.
    """
    if not content or not terms:
        return []

    lowered = content.lower()
    content_length = len(content)
    lowered_terms = [term.lower() for term in terms]
    scored: list[_ScoredWindow] = []

    for needle in lowered_terms:
        start = 0
        while True:
            match_index = lowered.find(needle, start)
            if match_index < 0:
                break

            window_start = max(0, match_index - SNIPPET_HALF_WIDTH)
            window_end = min(content_length, match_index + len(needle) + SNIPPET_HALF_WIDTH)
            # Scored over the whole neighbourhood, not just the term it is centred
            # on, so a window sitting next to the query's other terms outranks one
            # that repeats a single term.
            fragment = lowered[window_start:window_end]
            matches = sum(fragment.count(other) for other in lowered_terms)
            scored.append(_ScoredWindow(
                page_id=page_id,
                start=window_start,
                end=window_end,
                distinct_terms=sum(1 for other in lowered_terms if other in fragment),
                density=matches / max(window_end - window_start, 1),
            ))
            start = match_index + len(needle)

    return scored


def _absorb(chosen: list[tuple[int, _Window]], candidate: _ScoredWindow) -> int | None:
    """Widen an already-chosen window to cover `candidate`, returning the chars gained.

    None means the candidate overlaps nothing chosen and needs a slot of its own.
    Zero means it overlaps but cannot be absorbed — widening would breach
    `MAX_SNIPPET_CHARS` or run into the next chosen window — so it is dropped,
    its text being already on its way out.
    """
    same_page = [window for page_id, window in chosen if page_id == candidate.page_id]
    target = next(
        (w for w in same_page if candidate.start <= w.end and candidate.end >= w.start),
        None,
    )
    if target is None:
        return None

    start = min(target.start, candidate.start)
    end = max(target.end, candidate.end)
    collides = any(w is not target and start <= w.end and end >= w.start for w in same_page)
    if collides or end - start > MAX_SNIPPET_CHARS:
        return 0

    gained = (end - start) - (target.end - target.start)
    target.start, target.end = start, end
    return gained


def _assemble(
    scored: list[_ScoredWindow],
    contents: dict[int, str],
    path_by_id: dict[int, PageWithPath],
) -> list[SearchSnippet]:
    """Take the best windows within the per-page cap, count and budget limits.

    Windows arrive one per match, so they overlap. A window overlapping one
    already taken widens it instead of claiming a slot — the model gets the
    passage once, and the slots go to distinct parts of the corpus.
    """
    chosen: list[tuple[int, _Window]] = []
    per_page: dict[int, int] = {}
    used_chars = 0

    for candidate in scored:
        if len(chosen) >= MAX_SNIPPETS or used_chars >= RESPONSE_BUDGET_CHARS:
            break

        absorbed_chars = _absorb(chosen, candidate)
        if absorbed_chars is not None:
            used_chars += absorbed_chars
            continue

        if per_page.get(candidate.page_id, 0) >= MAX_SNIPPETS_PER_PAGE:
            continue

        # Budget against serialized size, not text alone — the handle and
        # breadcrumb are together about a third of a snippet's cost. Character
        # range over-states the text, which only shrinks as whitespace collapses.
        breadcrumb = _breadcrumb(path_by_id[candidate.page_id])
        cost = (candidate.end - candidate.start) + len(breadcrumb) + _JSON_OVERHEAD_PER_SNIPPET
        if chosen and used_chars + cost > RESPONSE_BUDGET_CHARS:
            break

        chosen.append((candidate.page_id, _Window(start=candidate.start, end=candidate.end)))
        per_page[candidate.page_id] = per_page.get(candidate.page_id, 0) + 1
        used_chars += cost

    snippets: list[SearchSnippet] = []
    for page_id, window in chosen:
        content = contents[page_id]
        path = path_by_id[page_id]
        snippets.append(SearchSnippet(
            snippet_id=encode_handle(page_id, window.start, window.end, content),
            page=_breadcrumb(path),
            text=_clean_snippet_text(content[window.start:window.end]),
            stale=_is_stale(path),
        ))
    return snippets


def _error_snippet(handle: str, message: str, path: PageWithPath | None = None) -> SearchSnippet:
    return SearchSnippet(
        snippet_id=handle,
        page=_breadcrumb(path) if path else "unknown",
        error=message,
    )


def _breadcrumb(path: PageWithPath) -> str:
    return f"{path.notebook_name} > {path.section_name} > {path.page_title or 'Untitled'}"


def _clean_snippet_text(text: str) -> str:
    """Collapse runs of whitespace into single spaces and trim."""
    return re.sub(r"\s+", " ", text).strip()


def _is_stale(path: PageWithPath) -> bool:
    """A page is stale if its own sync_status flags trouble or its notebook is mid-sync/failed."""
    if path.page_sync_status in (PageSyncStatus.SYNCING, PageSyncStatus.FAILED):
        return True
    if path.notebook_sync_status in (NotebookSyncStatus.SYNCING, NotebookSyncStatus.FAILED):
        return True
    return False
