import email
import io
import logging
import re
import xml.etree.ElementTree as ET
from typing import TypeAlias, TypeVar

import httpx
from fastapi import Request
from PIL import Image, ImageDraw
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from app.core.exceptions import GraphAPIError
from app.schemas import GraphNotebook, GraphPage, GraphPageContent, GraphSection

_BASE_URL = "https://graph.microsoft.com/v1.0"
# Beta URL uses /me/notes/ (not /me/onenote/) — different path from v1.0
_BETA_URL = "https://graph.microsoft.com/beta"
_INK_NODE_COMMENT = "<!-- InkNode is not supported -->"
_MAX_RETRIES = 5
_INK_OUTPUT_WIDTH = 2000
_INKML_NS = "http://www.w3.org/2003/InkML"

_M = TypeVar("_M")

InkPoint: TypeAlias = tuple[float, float]
InkStroke: TypeAlias = list[InkPoint]

logger = logging.getLogger(__name__)


def _is_rate_limited(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429


def _raise_graph_api_error(retry_state) -> None:
    raise GraphAPIError(f"Rate limit exceeded — failed after {_MAX_RETRIES} attempts")


def _extract_fullres_urls(html: str) -> list[str]:
    return re.findall(r'data-fullres-src="([^"]+)"', html)


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


def _render_strokes(strokes: list[InkStroke]) -> bytes | None:
    """Render ink strokes onto a white canvas. Returns PNG bytes, or None if strokes is empty."""
    if not strokes:
        return None

    all_x = [p[0] for stroke in strokes for p in stroke]
    all_y = [p[1] for stroke in strokes for p in stroke]
    min_x, max_x = min(all_x), max(all_x)
    min_y, max_y = min(all_y), max(all_y)

    coord_width = max_x - min_x or 1.0
    coord_height = max_y - min_y or 1.0
    scale = _INK_OUTPUT_WIDTH / coord_width
    output_height = max(1, int(coord_height * scale))

    img = Image.new("RGB", (_INK_OUTPUT_WIDTH, output_height), "white")
    draw = ImageDraw.Draw(img)

    stroke_width = max(3, int(scale * 30))  # ~0.3mm at typical OneNote DPI, min 3px

    for stroke in strokes:
        scaled = [(round((p[0] - min_x) * scale), round((p[1] - min_y) * scale)) for p in stroke]
        if len(scaled) == 1:
            x, y = scaled[0]
            r = stroke_width // 2
            draw.ellipse([x - r, y - r, x + r, y + r], fill="black")
        else:
            draw.line(scaled, fill="black", width=stroke_width, joint="curve")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class GraphClient:
    def __init__(self, http_client: httpx.AsyncClient) -> None:
        self._client = http_client

    def _headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    @retry(
        retry=retry_if_exception(_is_rate_limited),
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

        Beta URL uses /me/notes/ — confirmed different from v1.0 /me/onenote/ path.
        Response is multipart/form-data; Part 2 is application/inkml+xml.
        """
        try:
            url = f"{_BETA_URL}/me/notes/pages/{page_id}/content?includeInkML=true"
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
        """Fetch full page content including base images and rendered ink.

        Makes 1 API call for typed pages, 2 for pages with ink (v1.0 HTML + beta InkML).
        If the beta endpoint fails, ink_image is None and base images are still returned.
        """
        html = await self.get_page_content(access_token, page_id)

        image_urls = _extract_fullres_urls(html)
        base_images = [await self.get_page_image(access_token, url) for url in image_urls]

        if _INK_NODE_COMMENT not in html:
            return GraphPageContent(html=html, base_images=base_images, ink_image=None)

        inkml_xml = await self._get_inkml(access_token, page_id)
        ink_image = _render_strokes(_parse_inkml_strokes(inkml_xml)) if inkml_xml else None

        return GraphPageContent(html=html, base_images=base_images, ink_image=ink_image)


def get_graph_client(request: Request) -> GraphClient:
    return request.app.state.graph_client
