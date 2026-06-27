"""Throwaway analysis: download ONE OneNote PDF printout's source file and compare what we can get
out of it three ways — PyMuPDF embedded text, a rendered image (for human eyes), and Google Vision
OCR on that render. Goal: confirm PyMuPDF text is accurate, find pages where it fails (figure-only
slides), and see whether Vision recovers them — then decide how to *detect* those pages.

Budget-safe: the PDF is downloaded once and cached on disk; re-runs read the cache and make ZERO
Graph calls. Vision is only called on low-text pages (+ a couple text pages for comparison).

Run from backend/ with the venv + ephemeral PyMuPDF:

    cd backend
    uv run --with pymupdf python scripts/analyze_pdf_extraction.py --notebook "stat231(1)" --attachment "Chapter 1 Slides"

Outputs land in scripts/_probe_out/<safe-name>/ : the cached .pdf, per-page PNG renders, and a
text dump. Point the Read tool at the .pdf (with a page range) to eyeball pages yourself.
"""

import argparse
import asyncio
import logging
import re
from pathlib import Path

from bs4 import BeautifulSoup

from app.clients.graph_client import GraphClient
from app.clients.msal_client import get_msal_client
from app.clients.ocr_client import get_ocr_client
from app.core.database import AsyncSessionLocal
from app.core.encryption import decrypt
from app.repositories.microsoft_connection_repository import MicrosoftConnectionRepository

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("analyze")
logger.setLevel(logging.INFO)

OUT_ROOT = Path(__file__).parent / "_probe_out"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")


async def _download_pdf(notebook_filter: str, attachment_filter: str, dest: Path) -> str | None:
    """Find the first printout page whose PDF attachment matches and download it once. Returns the
    attachment filename, or None if not found. Skips entirely if dest already exists."""
    if dest.exists():
        logger.info("Using cached PDF: %s (%d bytes) — no Graph calls", dest, dest.stat().st_size)
        return dest.name

    async with AsyncSessionLocal() as session:
        connections = await MicrosoftConnectionRepository(session).get_all_active()
    if not connections:
        logger.error("No active Microsoft connections.")
        return None

    msal_client = get_msal_client()
    async with GraphClient() as graph:
        for connection in connections:
            try:
                token = msal_client.acquire_token_silent(decrypt(connection.encrypted_msal_token_cache)).access_token
            except Exception as error:
                logger.warning("Skipping connection %s (%s)", connection.id, error)
                continue
            connection_key = connection.id
            for notebook in await graph.get_notebooks(token, connection_key=connection_key):
                if notebook_filter.lower() not in notebook.display_name.lower():
                    continue
                for section in await graph.get_sections(token, notebook.id, connection_key=connection_key):
                    for page in await graph.get_pages(token, section.id, connection_key=connection_key):
                        html = await graph.get_page_content(token, page.id, connection_key=connection_key)
                        for obj in BeautifulSoup(html, "html.parser").find_all("object"):
                            name = obj.get("data-attachment") or ""
                            if (obj.get("type") or "").lower() == "application/pdf" \
                                    and attachment_filter.lower() in name.lower() and obj.get("data"):
                                logger.info("Found '%s' on page '%s' — downloading once...", name, getattr(page, "title", page.id))
                                pdf_bytes = await graph.get_page_image(
                                    token,
                                    obj["data"],
                                    connection_key=connection_key,
                                )
                                dest.write_bytes(pdf_bytes)
                                logger.info("Saved %d bytes to %s", len(pdf_bytes), dest)
                                return name
    logger.error("No PDF attachment matching '%s' in notebook '%s'.", attachment_filter, notebook_filter)
    return None


def _image_coverage(page) -> float:
    """Fraction of the page area covered by embedded raster images (0..1+). Free + local — this is
    the signal that catches 'text + chart' pages a char-count threshold alone would miss."""
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return 0.0
    covered = 0.0
    for img in page.get_images(full=True):
        for rect in page.get_image_rects(img[0]):
            covered += rect.width * rect.height
    return covered / page_area


def analyze(pdf_path: Path, out_dir: Path, dpi: int, low_text_threshold: int,
            sample_text_pages: int, skip_vision: bool) -> None:
    import fitz

    ocr = None if skip_vision else get_ocr_client()
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    text_dump = []
    low_text_pages: list[int] = []
    per_page_chars: list[int] = []
    per_page_coverage: list[float] = []

    with fitz.open(pdf_path) as document:
        page_count = document.page_count
        logger.info("PDF has %d pages; rendering at %d DPI", page_count, dpi)

        for index, page in enumerate(document):
            text = page.get_text("text").strip()
            per_page_chars.append(len(text))
            per_page_coverage.append(_image_coverage(page))
            text_dump.append(f"===== page {index + 1} ({len(text)} chars) =====\n{text}\n")
            if len(text) < low_text_threshold:
                low_text_pages.append(index)

        # Pages to render + OCR: every low-text page, plus the first few text pages for comparison.
        text_pages = [i for i in range(page_count) if i not in low_text_pages]
        compare_pages = sorted(set(low_text_pages + text_pages[:sample_text_pages]))

        logger.info("Low-text pages (<%d chars): %s",
                    low_text_threshold, [p + 1 for p in low_text_pages] or "none")

        for index in compare_pages:
            page = document[index]
            pixmap = page.get_pixmap(matrix=matrix)
            png_path = out_dir / f"page_{index + 1:03d}.png"
            pixmap.save(png_path)

            if skip_vision:
                vision_len = -1
            else:
                try:
                    vision_text = ocr.run_ocr(png_path.read_bytes())
                except Exception as error:
                    vision_text = f"<vision error: {error}>"
                vision_len = len(vision_text)

            kind = "LOW-TEXT" if index in low_text_pages else "text"
            logger.info(
                "page %3d [%-8s] pymupdf=%4d chars  img-coverage=%5.1f%%  vision=%s",
                index + 1, kind, per_page_chars[index], per_page_coverage[index] * 100,
                "skipped" if skip_vision else f"{vision_len} chars",
            )
            if not skip_vision and index in low_text_pages and vision_text:
                logger.info("    vision sample: %r", vision_text[:160].replace("\n", " "))

    (out_dir / "pymupdf_text.txt").write_text("".join(text_dump), encoding="utf-8")
    logger.info("Wrote per-page PyMuPDF text -> %s", out_dir / "pymupdf_text.txt")
    logger.info("Rendered PNGs -> %s  (Read the .pdf directly with a page range to eyeball pages)", out_dir)

    # Validate the proposed detector against the whole deck (free; no Graph/Vision):
    #   needs_ocr  =  (embedded text < threshold)  OR  (image coverage > coverage_threshold)
    # The coverage clause is what catches 'text + chart' pages that have plenty of bullet text but
    # also a figure with un-extractable baked-in labels — a char-count threshold alone misses them.
    coverage_threshold = 0.35
    total = len(per_page_chars)
    pure_text = low_text = mixed = 0
    mixed_pages: list[int] = []
    for index in range(total):
        is_low = per_page_chars[index] < low_text_threshold
        is_imagey = per_page_coverage[index] > coverage_threshold
        if is_low:
            low_text += 1
        elif is_imagey:
            mixed += 1
            mixed_pages.append(index + 1)
        else:
            pure_text += 1

    logger.info("=" * 70)
    logger.info("Detector preview (threshold=%d chars, coverage>%.0f%%):", low_text_threshold, coverage_threshold * 100)
    logger.info("  pure-text pages (PyMuPDF only, NO OCR):        %d", pure_text)
    logger.info("  low-text figure pages (OCR via render):        %d", low_text)
    logger.info("  text+figure pages (enough text BUT also OCR):  %d  %s",
                mixed, mixed_pages or "")
    logger.info("  -> OCR needed on %d/%d pages; the other %d are free PyMuPDF text.",
                low_text + mixed, total, pure_text)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notebook", required=True, help="notebook name substring")
    parser.add_argument("--attachment", required=True, help="PDF attachment name substring")
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--low-text-threshold", type=int, default=50, help="chars below which a page is 'low-text'")
    parser.add_argument("--sample-text-pages", type=int, default=3, help="how many text pages to also OCR for comparison")
    parser.add_argument("--no-vision", action="store_true", help="skip Vision calls (validate the local detector only)")
    args = parser.parse_args()

    out_dir = OUT_ROOT / _safe(f"{args.notebook}_{args.attachment}")
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / "source.pdf"

    name = await _download_pdf(args.notebook, args.attachment, pdf_path)
    if name is None:
        return
    analyze(pdf_path, out_dir, args.dpi, args.low_text_threshold, args.sample_text_pages, args.no_vision)


if __name__ == "__main__":
    asyncio.run(main())
