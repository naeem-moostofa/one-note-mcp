import asyncio
import email
import io
import logging
import random
import re
import time
import xml.etree.ElementTree as ET
from collections import deque
from typing import TypeAlias, TypeVar

import httpx
from bs4 import BeautifulSoup, Tag
from fastapi import Request
from PIL import Image, ImageDraw
from tenacity import RetryCallState, retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from app.core.config import settings
from app.core.exceptions import GraphAPIError
from app.schemas import GraphList, GraphNotebook, GraphPage, GraphPageContent, GraphPageElement, GraphSection

_BASE_URL = "https://graph.microsoft.com/v1.0"
# Beta uses the same /me/onenote/ path as v1.0, just against the beta base URL
_BETA_URL = "https://graph.microsoft.com/beta"
_INK_NODE_COMMENT = "<!-- InkNode is not supported -->"
# A throttle attempt now spends its wait inside the rate-limiter gate (acquire / wait_out_cooldown),
# which does NOT consume a tenacity attempt — so an attempt is burned only by a genuine probe-429
# (cooldown expired but OneNote still throttling). At the 60s cooldown cap that means ~15 probes can
# ride out roughly 15 minutes of sustained server-side throttle before a call gives up; anything
# longer falls through to the job-level retry (backoff up to 15 min × max_attempts). Cheap insurance
# since waiting is free here — the goal is completion, not speed.
_MAX_RETRIES = 15
_INKML_NS = "http://www.w3.org/2003/InkML"
# 1 HiMetric = 0.01 mm; at 96 DPI: px = himetric * 96 / 2540.
_HIMETRIC_TO_PX_BASE = 96.0 / 2540.0
# Oversampling factor for the composite — larger = more pixels per glyph = better OCR
# on dense handwriting. Clamped down if the page is large enough to exceed Vision's cap.
_TARGET_RENDER_SCALE = 2.0
# Vision's hard per-image cap is 75 MP (it silently downscales above that). We stay
# safely under to avoid quality loss; render_scale is clamped to keep canvas ≤ this.
_MAX_RENDER_PIXELS = 70_000_000
_BASE_INK_STROKE_WIDTH = 4  # px at 1x scale

# OneNote list endpoints default to 20 entries/page and cap $top at 100. The documented
# auto-paging via @odata.nextLink applies only when $top is omitted; with $top you page
# using $skip. We page by $skip at this size and cross-check against @odata.count. See _get_all.
_GRAPH_LIST_PAGE_SIZE = 100

_M = TypeVar("_M")

InkPoint: TypeAlias = tuple[float, float]
InkStroke: TypeAlias = list[InkPoint]
GraphConnectionKey: TypeAlias = int

logger = logging.getLogger(__name__)


_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
# 429 (throttled) and 503 (overloaded) additionally trip a connection-wide cooldown that
# pauses every in-flight + future request for that Microsoft account, not just the one that failed.
_THROTTLE_STATUS_CODES = {429, 503}

# Adaptive cooldown. OneNote sends no Retry-After, so on a throttle we compute our own pause
# and escalate it on consecutive throttles, decaying back after a quiet stretch.
_COOLDOWN_BASE_S = 1.0
_COOLDOWN_CAP_S = 60.0
_COOLDOWN_JITTER_S = 1.0
_COOLDOWN_MAX_LEVEL = 8
_RETRY_AFTER_CAP_S = 120.0
_THROTTLE_DECAY_S = 120.0

class _GraphRateLimiter:
    """Per-connection sliding-window rate limiter + adaptive throttle cooldown for OneNote/Graph.

    Honors both documented OneNote limits at once (per-minute and per-hour) by keeping a log
    of recent request timestamps and counting each window. A 429/503 sets a connection-local
    ``_paused_until`` deadline that every request observes in ``acquire()``, so one throttle
    backs off that Microsoft account rather than just the failing request. State is in-process,
    so it is correct only while a single process makes Graph calls for a given connection (see
    the rate-limit plan's deployment notes). The window/cooldown math lives in pure, lock-held
    helpers (``_reserve``, ``_apply_throttle``) so it can be unit-tested with an injected clock
    and no real sleeping.
    """

    def __init__(self, per_minute: int, per_hour: int, clock=time.monotonic) -> None:
        self._per_minute = per_minute
        self._per_hour = per_hour
        self._clock = clock
        self._request_times: deque[float] = deque()
        self._paused_until = 0.0
        self._throttle_level = 0
        self._last_throttle_at = 0.0
        self._lock = asyncio.Lock()
        # Minimum gap between two releases. During a cooldown the rolling minute window drains
        # to empty (no requests are recorded while paused), so without this every waiter would
        # clear the window check at once on reopen and stampede the still-throttled service.
        # Spacing releases at the per-minute rate turns that stampede into a ramp.
        self._min_spacing = 60.0 / per_minute if per_minute > 0 else 0.0

    def _reserve(self, now: float) -> float | None:
        """Decide whether a request may go now. Caller must hold ``self._lock``.

        Returns None and records the request when allowed, otherwise the number of seconds to
        wait before re-checking."""
        if now < self._paused_until:
            return self._paused_until - now

        # Forget the escalation once we've had a quiet stretch with no throttling.
        if self._throttle_level and now - self._last_throttle_at > _THROTTLE_DECAY_S:
            self._throttle_level = 0

        hour_cutoff = now - 3600
        while self._request_times and self._request_times[0] <= hour_cutoff:
            self._request_times.popleft()

        # Pace releases so a window emptied during a cooldown can't reopen as a burst. The most
        # recent timestamp is the deque tail (eviction only trims the head), so the next request
        # waits until one spacing interval after it.
        if self._request_times:
            earliest_next = self._request_times[-1] + self._min_spacing
            if now < earliest_next:
                return earliest_next - now

        minute_cutoff = now - 60
        count_minute = sum(1 for timestamp in self._request_times if timestamp > minute_cutoff)
        count_hour = len(self._request_times)

        if count_minute < self._per_minute and count_hour < self._per_hour:
            self._request_times.append(now)
            return None

        waits: list[float] = []
        if count_minute >= self._per_minute:
            oldest_in_minute = next(t for t in self._request_times if t > minute_cutoff)
            waits.append(oldest_in_minute + 60 - now)
        if count_hour >= self._per_hour:
            waits.append(self._request_times[0] + 3600 - now)
        return max(min(waits), 0.0)

    async def acquire(self) -> None:
        """Block until both rate windows have room and no cooldown is active."""
        while True:
            async with self._lock:
                wait = self._reserve(self._clock())
                if wait is None:
                    return
            await asyncio.sleep(max(wait, 0.0))

    async def wait_out_cooldown(self) -> None:
        """Block while a connection cooldown is active — re-checking *only* the pause, not the window.

        A request that already cleared ``acquire()`` can sit queued on the concurrency semaphore
        for seconds while earlier requests drain. If one of those 429s and arms the cooldown in
        that gap, this request would otherwise still hit the wire (acquire only gates on entry).
        Calling this right before the HTTP call closes that gap so a throttle stops the requests
        already past the gate, not just the next acquire() wave."""
        while True:
            async with self._lock:
                now = self._clock()
                if now >= self._paused_until:
                    return
                wait = self._paused_until - now
            await asyncio.sleep(max(wait, 0.0))

    def _apply_throttle(self, now: float, retry_after: float) -> None:
        """Escalate the connection cooldown after a throttle. Caller must hold ``self._lock``."""
        self._throttle_level = min(self._throttle_level + 1, _COOLDOWN_MAX_LEVEL)
        cooldown = min(_COOLDOWN_CAP_S, _COOLDOWN_BASE_S * 2 ** (self._throttle_level - 1))
        cooldown += random.uniform(0, _COOLDOWN_JITTER_S)
        cooldown = max(cooldown, retry_after)
        self._last_throttle_at = now
        self._paused_until = max(self._paused_until, now + cooldown)
        logger.info(
            "graph_cooldown level=%d pause_s=%.1f retry_after=%.1f",
            self._throttle_level, self._paused_until - now, retry_after,
        )

    async def register_throttle(self, retry_after: float = 0.0) -> None:
        async with self._lock:
            self._apply_throttle(self._clock(), retry_after)


class _GraphBudget:
    """One Microsoft connection's private rate window, cooldown, and concurrency cap."""

    def __init__(
        self,
        per_minute: int,
        per_hour: int,
        concurrency: int,
        clock=time.monotonic,
    ) -> None:
        self.limiter = _GraphRateLimiter(per_minute, per_hour, clock)
        self.semaphore = asyncio.Semaphore(concurrency)
        self.last_used = clock()
        self.active_requests = 0


class _GraphBudgetRegistry:
    """Lazy per-connection budget registry with amortized idle eviction."""

    def __init__(
        self,
        per_minute: int,
        per_hour: int,
        concurrency: int,
        *,
        idle_evict_s: float,
        evict_interval_s: float,
        clock=time.monotonic,
    ) -> None:
        self._per_minute = per_minute
        self._per_hour = per_hour
        self._concurrency = concurrency
        self._idle_evict_s = idle_evict_s
        self._evict_interval_s = evict_interval_s
        self._clock = clock
        self._budgets: dict[GraphConnectionKey, _GraphBudget] = {}
        self._lock = asyncio.Lock()
        self._last_evicted = clock()

    async def get(self, key: GraphConnectionKey) -> _GraphBudget:
        async with self._lock:
            now = self._clock()
            if now - self._last_evicted > self._evict_interval_s:
                self._evict_idle(now)
                self._last_evicted = now

            budget = self._budgets.get(key)
            if budget is None:
                budget = _GraphBudget(
                    self._per_minute,
                    self._per_hour,
                    self._concurrency,
                    self._clock,
                )
                self._budgets[key] = budget
            budget.last_used = now
            budget.active_requests += 1
            return budget

    async def release(self, key: GraphConnectionKey, budget: _GraphBudget) -> None:
        async with self._lock:
            current = self._budgets.get(key)
            if current is not budget:
                return
            budget.active_requests = max(0, budget.active_requests - 1)
            budget.last_used = self._clock()

    def _evict_idle(self, now: float) -> None:
        expired_keys = [
            key for key, budget in self._budgets.items()
            if budget.active_requests == 0 and now - budget.last_used > self._idle_evict_s
        ]
        for key in expired_keys:
            del self._budgets[key]


_budget_registry = _GraphBudgetRegistry(
    settings.SYNC_GRAPH_RATE_PER_MINUTE,
    settings.SYNC_GRAPH_RATE_PER_HOUR,
    settings.SYNC_GRAPH_CONCURRENCY,
    idle_evict_s=settings.SYNC_GRAPH_BUDGET_IDLE_EVICT_S,
    evict_interval_s=settings.SYNC_GRAPH_BUDGET_EVICT_INTERVAL_S,
)


def _is_retryable(error: BaseException) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in _RETRYABLE_STATUS_CODES
    # Transport-level failures (timeouts, connection/read/protocol errors) are transient under
    # load — these previously failed a page on the first occurrence.
    return isinstance(error, httpx.TransportError)


def _parse_retry_after(response: httpx.Response) -> float:
    """Seconds from a Retry-After header (delta-seconds form only), capped. OneNote omits it,
    but other Graph endpoints may send it and honoring it is correct and cheap."""
    value = response.headers.get("retry-after")
    if not value:
        return 0.0
    try:
        return min(float(value), _RETRY_AFTER_CAP_S)
    except ValueError:
        return 0.0  # HTTP-date form unsupported; fall back to the computed backoff


def _split_content_multipart(content_type: str, content: bytes) -> tuple[str | None, str | None]:
    """Parse beta page content into (html, inkml_xml), either of which may be absent."""
    raw = b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + content
    message = email.message_from_bytes(raw)

    html = None
    inkml = None
    for part in message.walk():
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue

        part_content_type = part.get_content_type()
        if part_content_type in ("text/html", "application/xhtml+xml"):
            html = payload.decode("utf-8", errors="replace")
        elif part_content_type == "application/inkml+xml":
            inkml = payload.decode("utf-8", errors="replace")

    return html, inkml


def _raise_graph_api_error(retry_state) -> None:
    raise GraphAPIError(f"Graph API unavailable — failed after {_MAX_RETRIES} attempts")


# Throttle/retry is the one Graph signal not already covered by httpx's own request logger
# (which logs method + URL + status per call), so it's the only bespoke log we keep. The
# request URL is logged directly — no need to re-derive an endpoint label from it.
def _log_graph_retry(retry_state: RetryCallState) -> None:
    error = retry_state.outcome.exception() if retry_state.outcome else None
    if not isinstance(error, httpx.HTTPStatusError):
        return

    retry_after = error.response.headers.get("retry-after")
    logger.info(
        "graph_retry url=%s status=%d attempt=%d next_sleep_s=%.1f retry_after=%s",
        error.request.url,
        error.response.status_code,
        retry_state.attempt_number,
        retry_state.next_action.sleep if retry_state.next_action else 0,
        retry_after or "-",
    )


_exponential_wait = wait_random_exponential(multiplier=1, max=60)


def _retry_wait(retry_state: RetryCallState) -> float:
    """Tenacity wait. Throttle codes wait ~0 here because the connection cooldown is applied
    at the rate limiter's ``acquire()`` gate (avoids double-sleeping the failing request);
    other retryable errors (502/504, transport timeouts) get per-request exponential backoff."""
    error = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in _THROTTLE_STATUS_CODES:
        return 0.0
    return _exponential_wait(retry_state)


def _parse_css_px(style: str, prop: str) -> float:
    match = re.search(rf"{prop}:\s*([\d.]+)px", style)
    return float(match.group(1)) if match else 0.0


def _attr_str(tag: Tag, name: str) -> str | None:
    """A single attribute value as a string, or None if absent.

    BeautifulSoup returns a list for multi-valued HTML attributes (e.g. ``class``). OneNote's
    attributes here are single-valued, but coerce defensively so callers always get a plain str."""
    value = tag.get(name)
    if isinstance(value, list):
        return " ".join(value)
    return value


def _parse_pdf_attachments(body: Tag) -> list[GraphPageElement]:
    """One ``pdf_attachment`` element per ``<object type="application/pdf">`` carrying a data URL.

    These objects are not necessarily positioned body children, so they're found across the whole
    body. Order relative to the positioned text/images does not matter — the sync service handles
    pdf_attachment elements separately from the composite/reading-order elements."""
    elements: list[GraphPageElement] = []
    for obj in body.find_all("object"):
        if not isinstance(obj, Tag):
            continue
        if (_attr_str(obj, "type") or "").lower() != "application/pdf":
            continue
        resource_url = _attr_str(obj, "data")
        if not resource_url:
            continue
        elements.append(GraphPageElement(
            kind="pdf_attachment",
            attachment_name=_attr_str(obj, "data-attachment"),
            resource_url=resource_url,
        ))
    return elements


def _parse_positioned_element(child: Tag, style: str) -> GraphPageElement | None:
    """Map one absolutely-positioned body child to a text/image element, or None to skip it."""
    img = child if child.name == "img" else child.find("img")
    if isinstance(img, Tag) and _attr_str(img, "data-fullres-src"):
        # Skip rasterized PDF-page images — they belong to a pdf_attachment we fetch once.
        if _attr_str(img, "data-options") == "printout":
            return None
        return GraphPageElement(
            kind="image",
            image_url=_attr_str(img, "data-fullres-src"),
            top=_parse_css_px(style, "top"),
            left=_parse_css_px(style, "left"),
            width=_parse_css_px(style, "width"),
            height=_parse_css_px(style, "height"),
        )

    if _INK_NODE_COMMENT in str(child):
        return None  # ink handled separately via InkML

    text = child.get_text(separator="\n", strip=True)
    if text:
        return GraphPageElement(kind="text", text=text)
    return None


def _parse_page_elements(html: str) -> list[GraphPageElement]:
    """Parse OneNote HTML body elements in visual reading order (sorted by CSS top/left).

    PDF "file printouts" are handled specially: OneNote keeps the source PDF as a single
    ``<object type="application/pdf">`` attachment *and* rasterizes every PDF page into its own
    ``<img data-options="printout">``. We emit one ``pdf_attachment`` element per object (fetched
    once, text pulled locally) and **skip** the per-page printout images that would otherwise cost
    one Graph ``$value`` request each. Genuinely loose images (no ``data-options="printout"``) are
    still emitted as ``image`` and fetched individually. See
    plans/attachment-fetch-optimization.md."""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body")
    if not isinstance(body, Tag):
        return []

    pdf_elements = _parse_pdf_attachments(body)

    positioned: list[tuple[float, float, GraphPageElement]] = []
    for child in body.children:
        if not isinstance(child, Tag):
            continue
        style = (_attr_str(child, "style") or "").replace(" ", "")
        if "position:absolute" not in style:
            continue
        element = _parse_positioned_element(child, style)
        if element is not None:
            positioned.append((_parse_css_px(style, "top"), _parse_css_px(style, "left"), element))

    positioned.sort(key=lambda positioned_element: (positioned_element[0], positioned_element[1]))
    return pdf_elements + [element for _, _, element in positioned]


def _parse_inkml_strokes(inkml_xml: str) -> list[InkStroke]:
    """Parse InkML XML into strokes. Each stroke is a list of (x, y) HiMetric coordinate pairs."""
    root = ET.fromstring(inkml_xml)

    strokes: list[InkStroke] = []
    for trace in root.iter(f"{{{_INKML_NS}}}trace"):
        if not trace.text:
            continue
        # Format: "X1 Y1 F1, X2 Y2 F2, ..." — space-separated per point, comma between points
        stroke: InkStroke = []
        for point_str in trace.text.strip().split(","):
            parts = point_str.strip().split()
            if len(parts) >= 2:
                try:
                    stroke.append((float(parts[0]), float(parts[1])))
                except ValueError:
                    continue
        if stroke:
            strokes.append(stroke)
    return strokes


def composite_page(
    elements: list[GraphPageElement],
    image_bytes_map: dict[str, bytes],
    ink_strokes: list[InkStroke],
) -> bytes | None:
    """Render images at their CSS positions, draw ink strokes on top, return PNG bytes.

    Returns PNG-encoded bytes for the composite canvas, or None if there is nothing
    to render (no images and no ink strokes). Typed text elements are ignored — callers
    extract those as plain text separately.

    Renders at _TARGET_RENDER_SCALE, clamping down so the canvas stays ≤ _MAX_RENDER_PIXELS
    (under Vision's 75 MP per-image cap, above which it silently downscales). One
    Vision call per page — no tiling.
    """
    image_elements = [element for element in elements if element.kind == "image" and element.image_url]

    if not image_elements and not ink_strokes:
        return None

    # Compute the natural (1x) canvas size first so we can pick a safe render scale.
    base_width = max((element.left + element.width for element in image_elements), default=0.0)
    base_height = max((element.top + element.height for element in image_elements), default=0.0)
    if ink_strokes:
        all_pixel_x = [point[0] * _HIMETRIC_TO_PX_BASE for stroke in ink_strokes for point in stroke]
        all_pixel_y = [point[1] * _HIMETRIC_TO_PX_BASE for stroke in ink_strokes for point in stroke]
        if all_pixel_x:
            base_width = max(base_width, max(all_pixel_x))
            base_height = max(base_height, max(all_pixel_y))
    base_width = max(base_width, 1.0)
    base_height = max(base_height, 1.0)

    # Pick the largest scale ≤ target that keeps us under Vision's per-image pixel cap.
    max_scale_for_vision = (_MAX_RENDER_PIXELS / (base_width * base_height)) ** 0.5
    render_scale = min(_TARGET_RENDER_SCALE, max_scale_for_vision)
    himetric_to_px = _HIMETRIC_TO_PX_BASE * render_scale
    stroke_width = max(1, int(_BASE_INK_STROKE_WIDTH * render_scale))

    canvas_width = max(int(base_width * render_scale), 1)
    canvas_height = max(int(base_height * render_scale), 1)
    if render_scale < _TARGET_RENDER_SCALE:
        logger.info(
            "composite_page: scaled to %.2fx (target %.2fx) to fit %dx%d under %d MP Vision cap",
            render_scale, _TARGET_RENDER_SCALE, canvas_width, canvas_height, _MAX_RENDER_PIXELS // 1_000_000,
        )

    canvas = Image.new("RGB", (canvas_width, canvas_height), "white")

    for element in image_elements:
        raw_image = image_bytes_map.get(element.image_url or "")
        if not raw_image:
            continue
        try:
            image = Image.open(io.BytesIO(raw_image)).convert("RGB")
            image_width = int(element.width * render_scale)
            image_height = int(element.height * render_scale)
            if image_width > 0 and image_height > 0:
                image = image.resize((image_width, image_height), Image.Resampling.LANCZOS)
            canvas.paste(image, (int(element.left * render_scale), int(element.top * render_scale)))
        except Exception:
            logger.warning("composite_page: failed to draw image at (%.0f, %.0f)", element.left, element.top)

    if ink_strokes:
        draw = ImageDraw.Draw(canvas)
        radius = stroke_width // 2
        for stroke in ink_strokes:
            points = [(round(point[0] * himetric_to_px), round(point[1] * himetric_to_px)) for point in stroke]
            if len(points) == 1:
                x_position, y_position = points[0]
                draw.ellipse(
                    [
                        x_position - radius,
                        y_position - radius,
                        x_position + radius,
                        y_position + radius,
                    ],
                    fill="black",
                )
            else:
                draw.line(points, fill="black", width=stroke_width, joint="curve")

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()


class GraphClient:
    def __init__(self, *, timeout: float = 30.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # GraphClient owns its transport. Pool sized above one connection's concurrency cap
        # so the per-connection cap is binding for today's serial worker. Tests inject a
        # transport (e.g. httpx.MockTransport) to avoid real network calls.
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(
                max_connections=settings.SYNC_GRAPH_CONCURRENCY * 2,
                max_keepalive_connections=settings.SYNC_GRAPH_CONCURRENCY,
            ),
            transport=transport,
        )

    async def __aenter__(self) -> "GraphClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._client.aclose()

    def _headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=_retry_wait,
        stop=stop_after_attempt(_MAX_RETRIES),
        before_sleep=_log_graph_retry,
        retry_error_callback=_raise_graph_api_error,
    )
    async def _get(
        self,
        url: str,
        access_token: str,
        *,
        connection_key: GraphConnectionKey,
    ) -> httpx.Response:
        # Pace against the OneNote rate limits and honor any active throttle cooldown BEFORE
        # taking a concurrency slot, so a backoff/cooldown wait releases the slot rather than
        # holding one of the 5 the whole time.
        budget = await _budget_registry.get(connection_key)
        try:
            await budget.limiter.acquire()
            async with budget.semaphore:
                # Re-check the cooldown after taking a concurrency slot: a sibling request may have
                # 429'd and armed the pause while we waited here, and acquire() only gates on entry.
                await budget.limiter.wait_out_cooldown()
                response = await self._client.get(url, headers=self._headers(access_token))
            # A throttle/overload response pauses this connection's budget before raising so
            # tenacity retries and sibling requests for the same account observe the cooldown.
            if response.status_code in _THROTTLE_STATUS_CODES:
                await budget.limiter.register_throttle(_parse_retry_after(response))
            response.raise_for_status()
            return response
        finally:
            await _budget_registry.release(connection_key, budget)

    async def _get_all(
        self,
        url: str,
        access_token: str,
        model: type[_M],
        *,
        connection_key: GraphConnectionKey,
    ) -> GraphList[_M]:
        """Enumerate a Graph collection with explicit ``$skip`` paging, returning the items
        plus whether the enumeration is complete.

        OneNote's documented auto-paging via ``@odata.nextLink`` applies only to requests
        that omit ``$top``; with ``$top`` you page using ``$skip``. We therefore page by
        ``$skip`` (page size ``_GRAPH_LIST_PAGE_SIZE``) until a short page, and cross-check
        the total against ``@odata.count`` (requested with ``$count=true`` on the first page).

        ``complete`` is False when the collected count is short of ``@odata.count`` or
        ``@odata.count`` is absent — i.e. we cannot *prove* we saw the whole collection. The
        caller must not delete-stale on an incomplete list: Graph can return a partial 200
        under throttling/degraded health (no 429), and treating absence as deletion would
        wipe live local rows. See plans/sync-stale-delete-data-loss.md."""
        items: list[_M] = []
        expected_count: int | None = None
        skip = 0
        while True:
            separator = "&" if "?" in url else "?"
            paged_url = f"{url}{separator}$top={_GRAPH_LIST_PAGE_SIZE}&$skip={skip}"
            if skip == 0:
                paged_url += "&$count=true"
            response = await self._get(paged_url, access_token, connection_key=connection_key)
            data = response.json()
            if skip == 0:
                raw_count = data.get("@odata.count")
                expected_count = raw_count if isinstance(raw_count, int) else None
            page = data.get("value", [])
            items.extend(model.model_validate(item) for item in page)  # type: ignore[attr-defined]
            if len(page) < _GRAPH_LIST_PAGE_SIZE:
                break
            skip += _GRAPH_LIST_PAGE_SIZE
        complete = expected_count is not None and len(items) >= expected_count
        return GraphList(items=items, complete=complete)

    async def _get_content_with_inkml(
        self,
        access_token: str,
        page_id: str,
        *,
        connection_key: GraphConnectionKey,
    ) -> tuple[str, str | None]:
        """Fetch page HTML and InkML in one beta call.

        Raises on HTTP failure so the caller can fall back to the v1.0 HTML endpoint.
        """
        url = f"{_BETA_URL}/me/onenote/pages/{page_id}/content?includeInkML=true"
        response = await self._get(url, access_token, connection_key=connection_key)
        content_type = response.headers.get("content-type", "")
        html, inkml_xml = _split_content_multipart(content_type, response.content)
        if html is None:
            if "multipart/" in content_type.lower():
                raise GraphAPIError("Beta page content response did not include an HTML part")
            html = response.text
        return html, inkml_xml

    async def get_notebooks(
        self,
        access_token: str,
        *,
        connection_key: GraphConnectionKey,
    ) -> GraphList[GraphNotebook]:
        return await self._get_all(
            f"{_BASE_URL}/me/onenote/notebooks",
            access_token,
            GraphNotebook,
            connection_key=connection_key,
        )

    async def get_sections(
        self,
        access_token: str,
        notebook_id: str,
        *,
        connection_key: GraphConnectionKey,
    ) -> GraphList[GraphSection]:
        return await self._get_all(
            f"{_BASE_URL}/me/onenote/notebooks/{notebook_id}/sections",
            access_token,
            GraphSection,
            connection_key=connection_key,
        )

    async def get_pages(
        self,
        access_token: str,
        section_id: str,
        *,
        connection_key: GraphConnectionKey,
    ) -> GraphList[GraphPage]:
        return await self._get_all(
            f"{_BASE_URL}/me/onenote/sections/{section_id}/pages",
            access_token,
            GraphPage,
            connection_key=connection_key,
        )

    async def get_page_content(
        self,
        access_token: str,
        page_id: str,
        *,
        connection_key: GraphConnectionKey,
    ) -> str:
        response = await self._get(
            f"{_BASE_URL}/me/onenote/pages/{page_id}/content",
            access_token,
            connection_key=connection_key,
        )
        return response.text

    async def get_page_image(
        self,
        access_token: str,
        resource_url: str,
        *,
        connection_key: GraphConnectionKey,
    ) -> bytes:
        """Fetch image bytes from a Graph resource URL extracted from page HTML."""
        response = await self._get(resource_url, access_token, connection_key=connection_key)
        return response.content

    async def get_page_content_with_ink(
        self,
        access_token: str,
        page_id: str,
        *,
        connection_key: GraphConnectionKey,
    ) -> GraphPageContent:
        """Fetch page content and parse elements in visual reading order (CSS top/left sorted).

        Makes 1 beta API call per page because the multipart beta response includes HTML and InkML.
        Falls back to the v1.0 HTML endpoint, without ink, only if the beta request fails.
        Images are returned as URLs — caller fetches them in order via get_page_image().
        """
        try:
            html, inkml_xml = await self._get_content_with_inkml(
                access_token,
                page_id,
                connection_key=connection_key,
            )
        except Exception as error:
            logger.warning(
                "Beta content fetch failed for page %s (%s) — falling back to v1.0 /content without ink",
                page_id, error,
            )
            html = await self.get_page_content(access_token, page_id, connection_key=connection_key)
            inkml_xml = None

        elements = _parse_page_elements(html)
        has_handwriting = _INK_NODE_COMMENT in html
        ink_strokes = _parse_inkml_strokes(inkml_xml) if inkml_xml else []

        return GraphPageContent(elements=elements, ink_strokes=ink_strokes, has_handwriting=has_handwriting)


def get_graph_client(request: Request) -> GraphClient:
    return request.app.state.graph_client
