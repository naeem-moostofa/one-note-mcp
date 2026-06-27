"""Throwaway probe: do PDF "file printouts" in your OneNote keep the original file as a single
attachment resource (one `$value` fetch) alongside the per-page rasterized `<img>` images?

If they do, we can fetch the whole PDF in ONE Graph request and extract text locally instead of
fetching N rasterized page-images. This script does NOT change anything — it reads page HTML and
reports the structure so we can decide.

It funnels every request through the real GraphClient, so the rate limiter applies. To protect
your hourly budget it stops after --max-pages page-content fetches (default 40) and lets you
filter to specific notebooks.

Run from the backend/ directory with the project venv. PYTHONPATH=. is required because `uv run`
puts the script's own dir (scripts/) on sys.path, not the backend root where the `app` package lives:

    cd backend
    PYTHONPATH=. uv run python scripts/probe_page_attachments.py --notebook "Research" --max-pages 30

Flags:
    --notebook SUBSTR   only scan notebooks whose name contains SUBSTR (case-insensitive); repeatable
    --max-pages N       stop after fetching N page bodies (budget guard; default 40)
    --dump-pages N      persist <img>/<object> attrs for up to N printout pages to _probe_out/ (no Graph cost)
    --all-pages         print a line for every scanned page, not just interesting ones
"""

import argparse
import asyncio
import json
import logging
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup

OUT_DIR = Path(__file__).parent / "_probe_out"

from app.clients.graph_client import GraphClient
from app.clients.msal_client import get_msal_client
from app.core.database import AsyncSessionLocal
from app.core.encryption import decrypt
from app.repositories.microsoft_connection_repository import MicrosoftConnectionRepository

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("probe")
logger.setLevel(logging.INFO)


def summarize_page_html(html: str) -> dict:
    """Pull out the bits that tell us whether a PDF printout kept its source file."""
    soup = BeautifulSoup(html, "html.parser")

    images = soup.find_all("img")
    fullres_images = [img for img in images if img.get("data-fullres-src")]

    objects = []
    for obj in soup.find_all("object"):
        objects.append({
            "data-attachment": obj.get("data-attachment"),
            "type": obj.get("type"),
            "data_url": obj.get("data"),  # the resource $value URL — fetch this for the original file
        })

    # OneNote sometimes annotates printout images; surface any attribute we don't already use so we
    # can learn the structure (e.g. data-render-original-src, data-index, data-id).
    interesting_img_attrs = Counter()
    for img in fullres_images:
        for attr in img.attrs:
            if attr not in ("src", "style", "alt"):
                interesting_img_attrs[attr] += 1

    return {
        "image_count": len(fullres_images),
        "objects": objects,
        "img_attrs": dict(interesting_img_attrs),
    }


def _trunc(value: str | None, limit: int = 140) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[:limit] + f"…(+{len(value) - limit})"


def dump_printout_elements(notebook: str, title: str, html: str) -> Path:
    """Persist every `<object>` and printout `<img>` with its attributes so the data-id ↔ object
    linkage can be inspected offline (the count-only summary can't prove which img maps to which
    object). Long URL values are truncated; we only need the linkage fields (data-id/data-index/…)."""
    soup = BeautifulSoup(html, "html.parser")

    objects = [{key: _trunc(obj.get(key)) for key in obj.attrs} for obj in soup.find_all("object")]
    images = [
        {key: _trunc(img.get(key)) for key in img.attrs if key not in ("style",)}
        for img in soup.find_all("img")
        if img.get("data-fullres-src")
    ]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(char if char.isalnum() else "_" for char in f"{notebook}_{title}")[:80]
    path = OUT_DIR / f"elements_{safe}.json"
    path.write_text(
        json.dumps({"notebook": notebook, "title": title, "objects": objects, "images": images}, indent=2),
        encoding="utf-8",
    )
    return path


def looks_like_pdf_printout(summary: dict) -> bool:
    if any((obj.get("type") or "").lower() == "application/pdf" for obj in summary["objects"]):
        return True
    if any("pdf" in (obj.get("data-attachment") or "").lower() for obj in summary["objects"]):
        return True
    # Many images + a file-attachment object is the classic printout shape.
    return summary["image_count"] >= 3 and bool(summary["objects"])


def extract_pdf_text(pdf_bytes: bytes) -> dict:
    """Open a PDF with PyMuPDF and measure how much *embedded* text it has.

    Born-digital slide exports have real text we can pull for free (no Vision). Scanned/image-only
    PDFs return ~nothing here, which means they'd need the render+OCR (Vision) fallback. The
    chars-per-page figure is the deciding signal.
    """
    import fitz  # PyMuPDF — imported lazily so the script still runs for the HTML-only scan

    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        page_count = document.page_count
        total_chars = 0
        empty_pages = 0
        sample = ""
        for index, page in enumerate(document):
            text = page.get_text("text").strip()
            total_chars += len(text)
            if not text:
                empty_pages += 1
            if index == 0:
                sample = text[:160].replace("\n", " ")

    chars_per_page = total_chars / page_count if page_count else 0
    return {
        "page_count": page_count,
        "total_chars": total_chars,
        "chars_per_page": round(chars_per_page, 1),
        "empty_pages": empty_pages,
        "text_bearing": chars_per_page >= 100,  # heuristic: real text vs scanned image
        "sample": sample,
    }


def _objects_display(objects: list[dict]) -> str:
    """Compact, non-noisy view of attachment objects (the raw $value URL is long, so hide it)."""
    if not objects:
        return "-"
    return ", ".join(f"{obj.get('data-attachment') or '?'} ({obj.get('type') or '?'})" for obj in objects)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notebook", action="append", default=[], help="notebook name substring filter (repeatable)")
    parser.add_argument("--max-pages", type=int, default=40, help="stop after N page-content fetches (budget guard)")
    parser.add_argument("--extract-pdfs", type=int, default=4, help="download & PyMuPDF-test at most N PDFs (budget guard)")
    parser.add_argument("--dump-pages", type=int, default=3, help="persist <img>/<object> attrs for at most N printout pages (no extra Graph cost)")
    parser.add_argument("--all-pages", action="store_true", help="print every scanned page, not just interesting ones")
    args = parser.parse_args()

    name_filters = [substr.lower() for substr in args.notebook]

    # Read connections, then close the DB session immediately — the Graph scan below runs for
    # minutes and Postgres would drop an idle connection held open across it. The response objects
    # are detached pydantic models, safe to use after close.
    async with AsyncSessionLocal() as session:
        connections = await MicrosoftConnectionRepository(session).get_all_active()
    if not connections:
        logger.error("No active Microsoft connections found — connect an account first.")
        return

    msal_client = get_msal_client()
    pages_fetched = 0
    printout_examples = 0
    pages_dumped = 0
    attachment_objects_seen = 0
    extractions: list[dict] = []  # {notebook, attachment, result|error}

    async with GraphClient() as graph:
            for connection in connections:
                try:
                    token_result = msal_client.acquire_token_silent(decrypt(connection.encrypted_msal_token_cache))
                except Exception as error:  # re-auth needed / bad cache — skip this connection
                    logger.warning("Skipping connection %s (token error: %s)", connection.id, error)
                    continue
                access_token = token_result.access_token
                connection_key = connection.id

                notebooks = await graph.get_notebooks(access_token, connection_key=connection_key)
                for notebook in notebooks:
                    if name_filters and not any(f in notebook.display_name.lower() for f in name_filters):
                        continue
                    logger.info("Notebook: %s", notebook.display_name)

                    sections = await graph.get_sections(access_token, notebook.id, connection_key=connection_key)
                    for section in sections:
                        pages = await graph.get_pages(access_token, section.id, connection_key=connection_key)
                        for page in pages:
                            if pages_fetched >= args.max_pages:
                                logger.info("Reached --max-pages=%d; stopping.", args.max_pages)
                                _report(printout_examples, attachment_objects_seen, pages_fetched, extractions)
                                return

                            html = await graph.get_page_content(access_token, page.id, connection_key=connection_key)
                            pages_fetched += 1
                            summary = summarize_page_html(html)
                            attachment_objects_seen += len(summary["objects"])
                            interesting = bool(summary["objects"]) or summary["image_count"] > 2
                            is_printout = looks_like_pdf_printout(summary)
                            if is_printout:
                                printout_examples += 1
                                if pages_dumped < args.dump_pages:
                                    title_for_dump = getattr(page, "title", None) or getattr(page, "display_name", page.id)
                                    dump_path = dump_printout_elements(notebook.display_name, title_for_dump, html)
                                    pages_dumped += 1
                                    logger.info("    dumped element attrs -> %s", dump_path)

                            title = getattr(page, "title", None) or getattr(page, "display_name", page.id)
                            if interesting or args.all_pages:
                                logger.info(
                                    "  [%s] images=%d objects=%s img_attrs=%s%s",
                                    title,
                                    summary["image_count"],
                                    _objects_display(summary["objects"]),
                                    summary["img_attrs"] or "-",
                                    "  <-- PDF printout" if is_printout else "",
                                )

                            # Download & PyMuPDF-test one PDF per printout page until the budget runs out.
                            if len(extractions) < args.extract_pdfs:
                                for obj in summary["objects"]:
                                    if len(extractions) >= args.extract_pdfs:
                                        break
                                    if (obj.get("type") or "").lower() != "application/pdf" or not obj.get("data_url"):
                                        continue
                                    attachment = obj.get("data-attachment") or "?"
                                    try:
                                        pdf_bytes = await graph.get_page_image(
                                            access_token,
                                            obj["data_url"],
                                            connection_key=connection_key,
                                        )
                                        result = extract_pdf_text(pdf_bytes)
                                        extractions.append({"notebook": notebook.display_name, "attachment": attachment, "result": result})
                                        logger.info(
                                            "    extracted '%s' (%d bytes): pages=%d chars/page=%.1f empty=%d -> %s",
                                            attachment, len(pdf_bytes), result["page_count"], result["chars_per_page"],
                                            result["empty_pages"],
                                            "TEXT-BEARING (PyMuPDF works)" if result["text_bearing"] else "image-only (needs Vision fallback)",
                                        )
                                        if result["sample"]:
                                            logger.info("      sample text: %r", result["sample"])
                                    except Exception as error:
                                        extractions.append({"notebook": notebook.display_name, "attachment": attachment, "error": str(error)})
                                        logger.warning("    failed to fetch/parse '%s': %s", attachment, error)

            _report(printout_examples, attachment_objects_seen, pages_fetched, extractions)


def _report(printout_examples: int, attachment_objects_seen: int, pages_fetched: int, extractions: list[dict]) -> None:
    logger.info("=" * 70)
    logger.info("Scanned %d page(s).", pages_fetched)
    logger.info("Pages that look like PDF printouts: %d", printout_examples)
    logger.info("Total <object> attachment elements seen: %d", attachment_objects_seen)

    if extractions:
        logger.info("-" * 70)
        logger.info("PyMuPDF extraction results (%d PDF(s) tested):", len(extractions))
        by_notebook: dict[str, list[str]] = {}
        for item in extractions:
            if "error" in item:
                verdict = f"ERROR: {item['error']}"
            else:
                verdict = "text-bearing" if item["result"]["text_bearing"] else "image-only (Vision)"
            by_notebook.setdefault(item["notebook"], []).append(f"{item['attachment']} -> {verdict}")
        for notebook, lines in by_notebook.items():
            logger.info("  %s:", notebook)
            for line in lines:
                logger.info("    - %s", line)
        text_ok = sum(1 for item in extractions if "result" in item and item["result"]["text_bearing"])
        logger.info(
            "Verdict: %d/%d PDFs are text-bearing (free local extraction); the rest would use the "
            "render+Vision fallback. A mix is fine — extract text when present, OCR when not.",
            text_ok, len(extractions),
        )

    if attachment_objects_seen:
        logger.info(
            "GOOD: <object> attachments are present — we can fetch the source file in ONE request "
            "instead of N rasterized page-images."
        )
    else:
        logger.info(
            "No <object> attachments found in the scanned pages. Either these printouts don't retain "
            "the source file, or none of the scanned pages had a file printout — try --notebook to "
            "target a notebook you know has a PDF printout."
        )


if __name__ == "__main__":
    asyncio.run(main())
