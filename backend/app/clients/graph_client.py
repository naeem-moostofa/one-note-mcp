import email
import io
import logging
import re
import xml.etree.ElementTree as ET
from typing import TypeAlias, TypeVar

import httpx
from bs4 import BeautifulSoup
from fastapi import Request
from PIL import Image, ImageDraw
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from app.core.exceptions import GraphAPIError
from app.schemas import GraphNotebook, GraphPage, GraphPageContent, GraphPageElement, GraphSection

_BASE_URL = "https://graph.microsoft.com/v1.0"
# Beta uses the same /me/onenote/ path as v1.0, just against the beta base URL
_BETA_URL = "https://graph.microsoft.com/beta"
_INK_NODE_COMMENT = "<!-- InkNode is not supported -->"
_MAX_RETRIES = 5
_INKML_NS = "http://www.w3.org/2003/InkML"
# 1 HiMetric = 0.01 mm; at 96 DPI: px = himetric * 96 / 2540.
_HIMETRIC_TO_PX_BASE = 96.0 / 2540.0
# Oversampling factor for the composite — larger = more pixels per glyph = better OCR
# on dense handwriting. We tile the composite for Vision rather than clamping scale.
_TARGET_RENDER_SCALE = 2.0
# Sanity cap on composite size to prevent runaway memory for pathologically tall pages.
# At 3 bytes/pixel RGB this is ~750 MB; if exceeded, _RENDER_SCALE is clamped down.
_MAX_COMPOSITE_PIXELS = 250_000_000
# Max pixels per OCR tile. Well under Vision's 75 MP cap (no auto-resize) and well under
# the 7.5 MB raw / 10 MB JSON request cap. Tiles >30 MP risk Vision silently downscaling.
_MAX_TILE_PIXELS = 30_000_000
_BASE_INK_STROKE_WIDTH = 4  # px at 1x scale

_M = TypeVar("_M")

InkPoint: TypeAlias = tuple[float, float]
InkStroke: TypeAlias = list[InkPoint]

logger = logging.getLogger(__name__)


_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in _RETRYABLE_STATUS_CODES


def _raise_graph_api_error(retry_state) -> None:
    raise GraphAPIError(f"Graph API unavailable — failed after {_MAX_RETRIES} attempts")


def _parse_css_px(style: str, prop: str) -> float:
    match = re.search(rf"{prop}:\s*([\d.]+)px", style)
    return float(match.group(1)) if match else 0.0


def _parse_page_elements(html: str) -> list[GraphPageElement]:
    """Parse OneNote HTML body elements in visual reading order (sorted by CSS top/left)."""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body")
    if not body:
        return []

    positioned: list[tuple[float, float, GraphPageElement]] = []

    for child in body.children:
        if not hasattr(child, "get"):
            continue
        style = child.get("style", "").replace(" ", "")
        if "position:absolute" not in style:
            continue

        top = _parse_css_px(style, "top")
        left = _parse_css_px(style, "left")

        img = child.find("img") if child.name != "img" else child
        if img and img.get("data-fullres-src"):
            width = _parse_css_px(style, "width")
            height = _parse_css_px(style, "height")
            positioned.append((top, left, GraphPageElement(
                kind="image",
                image_url=img["data-fullres-src"],
                top=top, left=left, width=width, height=height,
            )))
            continue

        if _INK_NODE_COMMENT in str(child):
            continue  # ink handled separately via InkML

        text = child.get_text(separator="\n", strip=True)
        if text:
            positioned.append((top, left, GraphPageElement(kind="text", text=text)))

    positioned.sort(key=lambda x: (x[0], x[1]))
    return [elem for _, _, elem in positioned]


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
) -> Image.Image | None:
    """Render images at their CSS positions then draw ink strokes on top.

    Returns a PIL Image for the composite canvas, or None if there is nothing to render
    (no images and no ink strokes). Typed text elements are ignored — callers extract
    those as plain text separately.

    Renders at _TARGET_RENDER_SCALE; only scales back if the page is enormous enough
    to threaten memory (>250 MP at 3 bytes/pixel = ~750 MB). For Vision API limits,
    use split_canvas_for_ocr() to tile the result.
    """
    image_elements = [e for e in elements if e.kind == "image" and e.image_url]

    if not image_elements and not ink_strokes:
        return None

    # Compute the natural (1x) canvas size first so we can pick a safe render scale.
    base_w = max((e.left + e.width for e in image_elements), default=0.0)
    base_h = max((e.top + e.height for e in image_elements), default=0.0)
    if ink_strokes:
        all_px_x = [p[0] * _HIMETRIC_TO_PX_BASE for stroke in ink_strokes for p in stroke]
        all_px_y = [p[1] * _HIMETRIC_TO_PX_BASE for stroke in ink_strokes for p in stroke]
        if all_px_x:
            base_w = max(base_w, max(all_px_x))
            base_h = max(base_h, max(all_px_y))
    base_w = max(base_w, 1.0)
    base_h = max(base_h, 1.0)

    # Pick the largest scale ≤ target that keeps us under the memory safety cap.
    max_scale_for_memory = (_MAX_COMPOSITE_PIXELS / (base_w * base_h)) ** 0.5
    render_scale = min(_TARGET_RENDER_SCALE, max_scale_for_memory)
    himetric_to_px = _HIMETRIC_TO_PX_BASE * render_scale
    stroke_width = max(1, int(_BASE_INK_STROKE_WIDTH * render_scale))

    canvas_w = max(int(base_w * render_scale), 1)
    canvas_h = max(int(base_h * render_scale), 1)
    if render_scale < _TARGET_RENDER_SCALE:
        logger.info(
            "composite_page: scaled to %.2fx (target %.2fx) to fit %dx%d under %d MP memory cap",
            render_scale, _TARGET_RENDER_SCALE, canvas_w, canvas_h, _MAX_COMPOSITE_PIXELS // 1_000_000,
        )

    canvas = Image.new("RGB", (canvas_w, canvas_h), "white")

    for elem in image_elements:
        raw = image_bytes_map.get(elem.image_url or "")
        if not raw:
            continue
        try:
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            w = int(elem.width * render_scale)
            h = int(elem.height * render_scale)
            if w > 0 and h > 0:
                img = img.resize((w, h), Image.LANCZOS)
            canvas.paste(img, (int(elem.left * render_scale), int(elem.top * render_scale)))
        except Exception:
            logger.warning("composite_page: failed to draw image at (%.0f, %.0f)", elem.left, elem.top)

    if ink_strokes:
        draw = ImageDraw.Draw(canvas)
        r = stroke_width // 2
        for stroke in ink_strokes:
            pts = [(round(p[0] * himetric_to_px), round(p[1] * himetric_to_px)) for p in stroke]
            if len(pts) == 1:
                x, y = pts[0]
                draw.ellipse([x - r, y - r, x + r, y + r], fill="black")
            else:
                draw.line(pts, fill="black", width=stroke_width, joint="curve")

    return canvas


def split_canvas_for_ocr(canvas: Image.Image, max_pixels: int = _MAX_TILE_PIXELS) -> list[Image.Image]:
    """Split a tall composite into vertical tiles each ≤ max_pixels.

    Picks split points at horizontal rows with the least ink density (within a search
    window around each evenly-spaced target row) so a tile boundary lands in whitespace
    between lines of text rather than slicing through a glyph.

    Returns [canvas] unchanged if the whole image already fits in one tile.
    """
    w, h = canvas.size
    total_pixels = w * h
    if total_pixels <= max_pixels:
        return [canvas]

    n_tiles = (total_pixels + max_pixels - 1) // max_pixels  # ceil
    nominal_tile_h = h // n_tiles

    grayscale = canvas.convert("L")
    # Treat anything below 200 as "ink"; sum the histogram up to that level per row band.
    INK_THRESHOLD = 200
    SAMPLE_BAND_HEIGHT = 5  # px — sample 5-row bands for stability, not single rows

    splits = [0]
    for i in range(1, n_tiles):
        target = i * nominal_tile_h
        radius = max(50, nominal_tile_h // 8)
        start = max(splits[-1] + SAMPLE_BAND_HEIGHT, target - radius)
        end = min(h - SAMPLE_BAND_HEIGHT, target + radius)
        if end <= start:
            splits.append(target)
            continue

        best_y = start
        best_ink = None
        for y in range(start, end, SAMPLE_BAND_HEIGHT):
            band = grayscale.crop((0, y, w, y + SAMPLE_BAND_HEIGHT))
            ink = sum(band.histogram()[:INK_THRESHOLD])
            if best_ink is None or ink < best_ink:
                best_ink = ink
                best_y = y
        splits.append(best_y)
    splits.append(h)

    tiles = [canvas.crop((0, splits[i], w, splits[i + 1])) for i in range(len(splits) - 1)]
    logger.info(
        "split_canvas_for_ocr: %dx%d (%d MP) -> %d tiles (heights: %s)",
        w, h, total_pixels // 1_000_000, len(tiles),
        [t.size[1] for t in tiles],
    )
    return tiles


def encode_png(image: Image.Image) -> bytes:
    """Encode a PIL Image to PNG bytes."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


class GraphClient:
    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._client = http_client

    def _headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_random_exponential(multiplier=1, max=60),
        stop=stop_after_attempt(_MAX_RETRIES),
        retry_error_callback=_raise_graph_api_error,
    )
    async def _get(self, url: str, access_token: str) -> httpx.Response:
        response = await self._client.get(url, headers=self._headers(access_token))
        response.raise_for_status()
        return response

    async def _get_all(self, url: str, access_token: str, model: type[_M]) -> list[_M]:
        items: list[_M] = []
        next_url: str | None = url
        while next_url:
            response = await self._get(next_url, access_token)
            data = response.json()
            for item in data.get("value", []):
                items.append(model.model_validate(item))  # type: ignore[attr-defined]
            next_url = data.get("@odata.nextLink")
        return items

    async def _get_inkml(self, access_token: str, page_id: str) -> str | None:
        """Fetch InkML XML via the beta endpoint. Returns None on any failure.

        Same /me/onenote/ path as v1.0 but against the beta base URL.
        Response is multipart/form-data; Part 2 is application/inkml+xml.
        """
        try:
            url = f"{_BETA_URL}/me/onenote/pages/{page_id}/content?includeInkML=true"
            response = await self._get(url, access_token)
            content_type = response.headers.get("content-type", "")
            raw = b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + response.content
            msg = email.message_from_bytes(raw)
            for part in msg.walk():
                if part.get_content_type() == "application/inkml+xml":
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        return payload.decode("utf-8", errors="replace")
            return None
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Failed to fetch InkML for page %s: %s — response body: %s",
                page_id, exc, exc.response.text,
            )
            return None
        except Exception as exc:
            logger.warning("Failed to fetch InkML for page %s: %s", page_id, exc)
            return None

    async def get_notebooks(self, access_token: str) -> list[GraphNotebook]:
        return await self._get_all(f"{_BASE_URL}/me/onenote/notebooks?$top=100", access_token, GraphNotebook)

    async def get_sections(self, access_token: str, notebook_id: str) -> list[GraphSection]:
        return await self._get_all(
            f"{_BASE_URL}/me/onenote/notebooks/{notebook_id}/sections?$top=100", access_token, GraphSection
        )

    async def get_pages(self, access_token: str, section_id: str) -> list[GraphPage]:
        return await self._get_all(
            f"{_BASE_URL}/me/onenote/sections/{section_id}/pages?$top=100", access_token, GraphPage
        )

    async def get_page_content(self, access_token: str, page_id: str) -> str:
        response = await self._get(f"{_BASE_URL}/me/onenote/pages/{page_id}/content", access_token)
        return response.text

    async def get_page_image(self, access_token: str, resource_url: str) -> bytes:
        """Fetch image bytes from a Graph resource URL extracted from page HTML."""
        response = await self._get(resource_url, access_token)
        return response.content

    async def get_page_content_with_ink(self, access_token: str, page_id: str) -> GraphPageContent:
        """Fetch page content and parse elements in visual reading order (CSS top/left sorted).

        Makes 1 API call for typed pages, 2 for pages with ink (v1.0 HTML + beta InkML).
        Images are returned as URLs — caller fetches them in order via get_page_image().
        If the beta endpoint fails, ink_image is None but other content is still returned.
        """
        html = await self.get_page_content(access_token, page_id)
        has_handwriting = _INK_NODE_COMMENT in html
        elements = _parse_page_elements(html)

        if not has_handwriting:
            return GraphPageContent(elements=elements, ink_strokes=[], has_handwriting=False)

        inkml_xml = await self._get_inkml(access_token, page_id)
        ink_strokes = _parse_inkml_strokes(inkml_xml) if inkml_xml else []

        return GraphPageContent(elements=elements, ink_strokes=ink_strokes, has_handwriting=True)


def get_graph_client(request: Request) -> GraphClient:
    return request.app.state.graph_client
