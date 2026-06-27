"""Local text extraction from PDF "file printout" attachments.

OneNote rasterizes every PDF page into its own embedded image; fetching those costs one Graph
``$value`` request each. Instead we fetch the source PDF once (see graph_client `_parse_page_elements`
/ `pdf_attachment`) and pull text locally with PyMuPDF. Digital slide decks carry a real, lossless
embedded text layer (free); only pages with too little extractable text fall back to Vision OCR of a
locally rendered page. See plans/attachment-fetch-optimization.md.

This module is pure PyMuPDF/CPU work with no network — `extract_pdf` is meant to run inside
`asyncio.to_thread` (a `fitz.Document` is not thread-safe, so one document stays on one thread). The
caller runs the OCR calls (`renders_to_ocr`) and feeds the results back into `merge_pdf_text`.
"""

import logging
from dataclasses import dataclass
from typing import cast

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

# MuPDF's C core writes diagnostics (e.g. the benign "format error: No common ancestor in structure
# tree" on tagged PDFs) straight to the process stderr, bypassing Python logging. Route them into the
# standard logging system under the `pymupdf` logger instead — they arrive at DEBUG, so they're
# silent at the app's INFO level but captured and turn-up-able when actually debugging a PDF.
fitz.set_messages(pylogging=True)

# PDF user space is 72 units per inch, so get_pixmap() with no matrix renders at 72 DPI.
_PDF_POINTS_PER_INCH = 72.0


@dataclass
class PdfExtraction:
    """Result of extracting one PDF locally.

    `page_texts` is index-aligned to the PDF pages (embedded text, always kept). `renders_to_ocr`
    holds `(page_index, png_bytes)` for the pages the detector flagged — the caller OCRs these and
    passes the results, keyed by page index, to `merge_pdf_text`.
    """

    page_texts: list[str]
    renders_to_ocr: list[tuple[int, bytes]]


def extract_pdf(
    pdf_bytes: bytes,
    *,
    render_dpi: int,
    text_threshold: int,
) -> PdfExtraction:
    """Open a PDF from bytes, pull per-page embedded text, and render the pages that need OCR.

    A page is sent to OCR only when its embedded text is shorter than ``text_threshold`` — i.e. it's
    a figure/scan page with no usable text layer. (An earlier image-coverage heuristic was dropped:
    it summed overlapping image rects and so flagged text-rich decorated slides — e.g. decks with a
    per-page watermark — for needless OCR, replacing clean embedded text with noisy OCR.)

    Pure CPU/PyMuPDF — no network. Rendering is done here (still on the worker thread, while the
    document is open) so the caller only deals with PNG bytes, never the thread-unsafe document.
    """
    zoom = render_dpi / _PDF_POINTS_PER_INCH
    matrix = fitz.Matrix(zoom, zoom)

    page_texts: list[str] = []
    renders_to_ocr: list[tuple[int, bytes]] = []

    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        for index in range(document.page_count):
            page = document.load_page(index)
            # PyMuPDF's get_text() stub returns str | list | dict; the "text" mode always yields str.
            text = cast(str, page.get_text("text"))
            page_texts.append(text)
            if len(text.strip()) < text_threshold:
                pixmap = page.get_pixmap(matrix=matrix)
                renders_to_ocr.append((index, pixmap.tobytes("png")))

    logger.info(
        "PDF extracted: %d page(s), %d need OCR (dpi=%d)",
        len(page_texts), len(renders_to_ocr), render_dpi,
    )
    return PdfExtraction(page_texts=page_texts, renders_to_ocr=renders_to_ocr)


def merge_pdf_text(page_texts: list[str], ocr_by_index: dict[int, str]) -> str:
    """Combine embedded text with OCR text, in page order, deduping repeated lines.

    The embedded text is always kept. For an OCR'd page, only OCR lines not already present in that
    page's embedded text are appended — Vision re-reads text the PDF already carries, so this avoids
    doubling. Lines are matched on their stripped form.
    """
    parts: list[str] = []
    for index, page_text in enumerate(page_texts):
        if page_text.strip():
            parts.append(page_text.strip())

        ocr_text = ocr_by_index.get(index, "")
        if not ocr_text.strip():
            continue
        existing = {line.strip() for line in page_text.splitlines() if line.strip()}
        new_lines = [line for line in ocr_text.splitlines() if line.strip() and line.strip() not in existing]
        if new_lines:
            parts.append("\n".join(new_lines))

    return "\n\n".join(parts)
