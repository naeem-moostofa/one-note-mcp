"""
Page search.

Two-pass algorithm:
1. Postgres FTS via `websearch_to_tsquery` — fast, precise, catches everything
   in typed text and well-OCR'd words.
2. Trigram word-similarity fallback for query terms that produced no FTS hits
   — handles OCR garbling like `painters` ↔ `pointers`.

Pages are ranked by (FTS rank + 0.5 × max trigram similarity) and capped.
For each surviving page, character windows around match offsets are extracted,
overlapping windows merged, and the result list capped to a per-page limit.

This service is wrapped by the MCP `search_pages` tool. See
`plans/search-service-plan.md` for the design.
"""

from __future__ import annotations

import re

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NotebookSyncStatus, PageSyncStatus
from app.repositories.page_repository import PageRepository
from app.schemas import (
    PageFTSHit,
    PageTrgmHit,
    PageWithPath,
    SearchHit,
    SearchSnippet,
)


# ---- Tunables -------------------------------------------------------------

# Hard caps the MCP tool advertises. Service clamps to these so a buggy caller
# can't ask for 1000-char snippets times 50 snippets times 100 pages.
MAX_SEARCH_SIZE = 250
MAX_PAGES_LIMIT = 20
MAX_SNIPPETS_PER_PAGE_LIMIT = 10

# Trigram word_similarity cutoff for the fuzzy fallback. 0.3 is permissive
# enough to catch OCR drift (`painters` vs `pointers` scores ~0.5) without
# flooding results with noise. Tune post-launch.
TRGM_THRESHOLD = 0.3

# Weight of trigram similarity relative to FTS rank when combining scores.
# FTS rank is unbounded but typically small (< 1.0 in practice on short
# queries); word_similarity is in [0, 1]. Keeping trigram at half-weight
# lets exact FTS hits dominate ties while still letting fuzzy-only matches
# surface when FTS finds nothing.
TRGM_RANK_WEIGHT = 0.5

# We over-fetch from each pass by this factor so the cross-pass merge has
# room to re-rank. `max_pages * CANDIDATE_FACTOR` rows come back from each
# query and get whittled down to `max_pages` after combining scores.
CANDIDATE_FACTOR = 3


# ---- Internal types -------------------------------------------------------


class _RankedPage(BaseModel):
    """Per-page accumulator across the FTS and trigram passes."""
    page_id: int
    content: str
    fts_rank: float = 0.0
    trgm_score: float = 0.0

    @property
    def combined_score(self) -> float:
        return self.fts_rank + TRGM_RANK_WEIGHT * self.trgm_score


class _Window(BaseModel):
    """Half-open character window into pages.content."""
    start: int
    end: int


# ---- Service --------------------------------------------------------------


class SearchService:
    def __init__(self, session: AsyncSession) -> None:
        self._pages = PageRepository(session)

    async def search(
        self,
        query: str,
        notebook_ids: list[int],
        search_size: int = 80,
        max_pages: int = 10,
        max_snippets_per_page: int = 5,
    ) -> list[SearchHit]:
        """
        Search pages across the given notebook scope.

        `notebook_ids` is required and is expected to already be intersected with
        the caller's MCP-connection-allowed scope by the MCP tool — this service
        treats it as authoritative.
        """
        query = (query or "").strip()
        if not query or not notebook_ids:
            return []

        # 1. Clamp params.
        search_size = max(1, min(search_size, MAX_SEARCH_SIZE))
        max_pages = max(1, min(max_pages, MAX_PAGES_LIMIT))
        max_snippets_per_page = max(1, min(max_snippets_per_page, MAX_SNIPPETS_PER_PAGE_LIMIT))
        candidate_limit = max_pages * CANDIDATE_FACTOR

        # 2. FTS pass.
        fts_hits = await self._pages.search_fts(notebook_ids, query, candidate_limit)

        # 3. Identify which raw query terms FTS likely missed. Cheap substring
        #    scan of the returned content — if no FTS hit contains the literal
        #    term anywhere, the term probably needs the trigram fallback.
        terms = _tokenize_terms(query)
        missed_terms = _terms_missing_from_fts(terms, fts_hits)

        # 4. Trigram fallback.
        trgm_hits: list[PageTrgmHit] = []
        if missed_terms:
            trgm_hits = await self._pages.search_trgm(
                notebook_ids, missed_terms, TRGM_THRESHOLD, candidate_limit
            )

        if not fts_hits and not trgm_hits:
            return []

        # 5. Merge + rank.
        ranked = _merge_ranked(fts_hits, trgm_hits)

        # 6. Top max_pages by combined score.
        ranked.sort(key=lambda p: p.combined_score, reverse=True)
        top = ranked[:max_pages]

        # 7. Resolve notebook/section path + sync status for the survivors.
        # Index by page_id so the assembly loop can attach paths in O(1) while
        # iterating `top` in score order.
        paths = await self._pages.get_pages_with_path([p.page_id for p in top])
        path_by_id = {p.page_id: p for p in paths}

        # 8. Build snippets + assemble SearchHits.
        # Preserve the ranked order from step 6.
        hits: list[SearchHit] = []
        for ranked_page in top:
            path = path_by_id.get(ranked_page.page_id)
            if path is None:
                # Page was deleted between the search query and the path lookup —
                # drop it silently rather than emitting half a hit.
                continue
            snippets = _build_snippets(
                content=ranked_page.content,
                terms=terms,
                search_size=search_size,
                max_snippets=max_snippets_per_page,
            )
            hits.append(SearchHit(
                page_id=path.page_id,
                page_title=path.page_title,
                section_name=path.section_name,
                notebook_id=path.notebook_id,
                notebook_name=path.notebook_name,
                snippets=snippets,
                stale=_is_stale(path),
            ))

        return hits


# ---- Helpers --------------------------------------------------------------


# Word-ish: letters, digits, underscore. Anything else acts as a separator.
# Strips FTS operators (`&`, `|`, `!`, parens, quotes) along with punctuation
# so the substring + trigram passes don't carry tsquery syntax with them.
_TERM_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize_terms(query: str) -> list[str]:
    """Split a user query into bare terms for substring + trigram use."""
    # Lowercase here so callers can do case-sensitive substring scans against
    # already-lowercased content slices when building snippets.
    return [m.group(0) for m in _TERM_RE.finditer(query) if len(m.group(0)) >= 2]


def _terms_missing_from_fts(terms: list[str], fts_hits: list[PageFTSHit]) -> list[str]:
    """Return terms whose literal form doesn't appear in any FTS hit's content."""
    if not terms:
        return []
    if not fts_hits:
        return list(terms)

    lowered_contents = [hit.content.lower() for hit in fts_hits]
    missed: list[str] = []
    for term in terms:
        needle = term.lower()
        if not any(needle in body for body in lowered_contents):
            missed.append(term)
    return missed


def _merge_ranked(
    fts_hits: list[PageFTSHit],
    trgm_hits: list[PageTrgmHit],
) -> list[_RankedPage]:
    """Combine FTS + trigram hits into one ranked-page accumulator per page_id."""
    by_id: dict[int, _RankedPage] = {}

    for hit in fts_hits:
        by_id[hit.page_id] = _RankedPage(
            page_id=hit.page_id,
            content=hit.content,
            fts_rank=hit.rank,
        )

    for hit in trgm_hits:
        existing = by_id.get(hit.page_id)
        if existing is None:
            by_id[hit.page_id] = _RankedPage(
                page_id=hit.page_id,
                content=hit.content,
                trgm_score=hit.score,
            )
        else:
            # If both passes returned this page, keep the higher trigram score.
            existing.trgm_score = max(existing.trgm_score, hit.score)

    return list(by_id.values())


def _build_snippets(
    content: str,
    terms: list[str],
    search_size: int,
    max_snippets: int,
) -> list[SearchSnippet]:
    """
    Find all match offsets for `terms` in `content`, expand each to a
    [off - search_size, off + len(term) + search_size] window, merge overlapping
    windows, and return the first `max_snippets`.

    Falls back to a head snippet when no literal term matches (typical for a
    page that surfaced only via trigram fuzzy match).
    """
    if not content:
        return []

    windows = _collect_windows(content, terms, search_size)

    if not windows:
        # Trigram-only hit: no literal match to anchor a window. Return the
        # head of the page so the caller sees *something*. A future version
        # could locate the trigram-matched substring directly; for V1 this
        # is good enough.
        end = min(len(content), 2 * search_size)
        return [SearchSnippet(
            text=_clean_snippet_text(content[0:end]),
            start_offset=0,
        )]

    merged = _merge_windows(windows)
    # Keep the longest (most-context-rich) windows first — they're the merged
    # clusters covering several nearby matches.
    merged.sort(key=lambda w: w.end - w.start, reverse=True)
    merged = merged[:max_snippets]
    # Then return them in document order so the caller reads top-to-bottom.
    merged.sort(key=lambda w: w.start)

    return [
        SearchSnippet(
            text=_clean_snippet_text(content[w.start:w.end]),
            start_offset=w.start,
        )
        for w in merged
    ]


def _collect_windows(content: str, terms: list[str], search_size: int) -> list[_Window]:
    """One window per occurrence of each term, expanded by `search_size` on each side."""
    if not terms:
        return []

    lowered = content.lower()
    content_len = len(content)
    windows: list[_Window] = []

    for term in terms:
        needle = term.lower()
        if not needle:
            continue
        start = 0
        while True:
            idx = lowered.find(needle, start)
            if idx < 0:
                break
            window_start = max(0, idx - search_size)
            window_end = min(content_len, idx + len(needle) + search_size)
            windows.append(_Window(start=window_start, end=window_end))
            # Advance past this match. Stepping by one rather than len(needle)
            # would re-find overlapping matches like `aa` in `aaaa`, which is
            # noise — `idx + len(needle)` is the right step here.
            start = idx + len(needle)

    return windows


def _merge_windows(windows: list[_Window]) -> list[_Window]:
    """Merge any windows that overlap or touch (end_a >= start_b)."""
    if not windows:
        return []

    windows.sort(key=lambda w: w.start)
    merged: list[_Window] = [windows[0]]
    for current in windows[1:]:
        last = merged[-1]
        if current.start <= last.end:
            # Overlap or touch — absorb.
            last.end = max(last.end, current.end)
        else:
            merged.append(current)
    return merged


def _clean_snippet_text(text: str) -> str:
    """Collapse runs of whitespace so snippets stay compact for the LLM."""
    return re.sub(r"\s+", " ", text).strip()


def _is_stale(path: PageWithPath) -> bool:
    """A page is stale if its own sync_status flags trouble or its notebook is mid-sync/failed."""
    if path.page_sync_status in (PageSyncStatus.SYNCING, PageSyncStatus.FAILED):
        return True
    if path.notebook_sync_status in (NotebookSyncStatus.SYNCING, NotebookSyncStatus.FAILED):
        return True
    return False
