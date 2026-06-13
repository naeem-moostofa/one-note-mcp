"""Inspect the composite + OCR pipeline for a single page.

Resolves a page from the local DB (by id or title), reuses the production
sync code to fetch HTML + InkML + images, builds the composite PNG that
would be sent to Google Cloud Vision, and (optionally) runs OCR on it.

All artifacts are written to disk so you can verify what the OCR sees.

Examples:
    cd backend
    python -m scripts.test_ocr --page-id 42
    python -m scripts.test_ocr --page-title "Lecture 3"
    python -m scripts.test_ocr --page-title "Lecture 3" --notebook "CS 101"
    python -m scripts.test_ocr --page-id 42 --skip-ocr
"""

import argparse
import asyncio
import io
import json
import logging
import re
from pathlib import Path

import httpx
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.graph_client import GraphClient, composite_page
from app.clients.msal_client import get_msal_client
from app.clients.ocr_client import get_ocr_client
from app.core.database import AsyncSessionLocal
from app.core.encryption import decrypt
from app.core.exceptions import MSALAuthError
from app.models import MicrosoftConnection, Notebook, Page, Section

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


class _ResolvedPage(BaseModel):
    page_db_id: int
    page_onenote_id: str
    page_title: str | None
    section_name: str
    notebook_name: str
    user_id: int


def _safe_filename(s: str, max_len: int = 50) -> str:
    cleaned = re.sub(r"[^\w\-]+", "_", s).strip("_")
    return (cleaned or "untitled")[:max_len]


async def _resolve_page(
    session: AsyncSession,
    page_id: int | None,
    page_title: str | None,
    notebook: str | None,
) -> _ResolvedPage:
    statement = (
        select(Page, Section, Notebook)
        .join(Section, Page.section_id == Section.id)
        .join(Notebook, Section.notebook_id == Notebook.id)
    )

    if page_id is not None:
        statement = statement.where(Page.id == page_id)
    else:
        assert page_title is not None
        statement = statement.where(Page.title.ilike(page_title))
        if notebook is not None:
            if notebook.isdigit():
                statement = statement.where(Notebook.id == int(notebook))
            else:
                statement = statement.where(Notebook.display_name.ilike(notebook))

    rows = (await session.execute(statement)).all()

    if not rows:
        raise SystemExit("No matching page found in DB.")
    if len(rows) > 1:
        listing = "\n".join(
            f"  - page_id={p.id} title={p.title!r} section={s.display_name!r} notebook={n.display_name!r}"
            for p, s, n in rows
        )
        raise SystemExit(f"Multiple pages match — narrow with --page-id or --notebook:\n{listing}")

    page, section, notebook_row = rows[0]
    return _ResolvedPage(
        page_db_id=page.id,
        page_onenote_id=page.onenote_id,
        page_title=page.title,
        section_name=section.display_name,
        notebook_name=notebook_row.display_name,
        user_id=notebook_row.user_id,
    )


async def _acquire_token(session: AsyncSession, user_id: int) -> str:
    conn = await session.scalar(
        select(MicrosoftConnection).where(MicrosoftConnection.user_id == user_id)
    )
    if conn is None:
        raise SystemExit(f"No Microsoft connection for user {user_id}")
    try:
        result = get_msal_client().acquire_token_silent(decrypt(conn.encrypted_msal_token_cache))
    except MSALAuthError:
        raise SystemExit("Re-auth required — reconnect your Microsoft account via the app first.")
    return result.access_token


async def main(
    page_id: int | None,
    page_title: str | None,
    notebook: str | None,
    output_dir: Path,
    skip_ocr: bool,
) -> None:
    async with AsyncSessionLocal() as session:
        resolved = await _resolve_page(session, page_id, page_title, notebook)
        logger.info(
            "Resolved: id=%d title=%r section=%r notebook=%r",
            resolved.page_db_id, resolved.page_title, resolved.section_name, resolved.notebook_name,
        )
        access_token = await _acquire_token(session, resolved.user_id)

    target_dir = output_dir / f"{resolved.page_db_id}_{_safe_filename(resolved.page_title or resolved.page_onenote_id)}"
    target_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Output dir: %s", target_dir.resolve())

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http_client:
        graph_client = GraphClient(http_client)

        logger.info("Fetching page HTML + InkML from Graph...")
        page_content = await graph_client.get_page_content_with_ink(access_token, resolved.page_onenote_id)

        text_elements = [e for e in page_content.elements if e.kind == "text" and e.text]
        image_elements = [e for e in page_content.elements if e.kind == "image" and e.image_url]

        logger.info(
            "Parsed: %d text block(s), %d image(s), has_handwriting=%s, ink_strokes=%d",
            len(text_elements), len(image_elements), page_content.has_handwriting, len(page_content.ink_strokes),
        )
        if page_content.has_handwriting and not page_content.ink_strokes:
            logger.warning("InkML fetch failed — ink will not appear in composite")

        image_bytes_map: dict[str, bytes] = {}
        if image_elements:
            logger.info("Fetching %d image(s) in parallel...", len(image_elements))
            urls = [e.image_url for e in image_elements]
            results = await asyncio.gather(
                *[graph_client.get_page_image(access_token, url) for url in urls],
                return_exceptions=True,
            )
            for url, result in zip(urls, results):
                if isinstance(result, Exception):
                    logger.warning("  Image fetch failed: %s", result)
                else:
                    image_bytes_map[url] = result  # type: ignore[assignment]
            logger.info("  Got %d/%d images", len(image_bytes_map), len(image_elements))

    composite_bytes = composite_page(page_content.elements, image_bytes_map, page_content.ink_strokes)

    composite_size: tuple[int, int] | None = None
    if composite_bytes is None:
        logger.warning("No composite produced (no images and no ink). Nothing to OCR.")
    else:
        composite_size = Image.open(io.BytesIO(composite_bytes)).size
        (target_dir / "composite.png").write_bytes(composite_bytes)
        logger.info(
            "Wrote composite.png — %dx%d (%.1f MP, %d KB)",
            composite_size[0], composite_size[1],
            composite_size[0] * composite_size[1] / 1_000_000,
            len(composite_bytes) // 1024,
        )

    typed_text = "\n\n".join(e.text for e in text_elements if e.text)
    (target_dir / "typed-text.txt").write_text(typed_text, encoding="utf-8")
    logger.info("Wrote typed-text.txt (%d chars)", len(typed_text))

    ocr_text = ""
    if composite_bytes is not None and not skip_ocr:
        logger.info("Calling Google Cloud Vision on composite...")
        ocr_text = await asyncio.to_thread(get_ocr_client().run_ocr, composite_bytes)
        (target_dir / "ocr.txt").write_text(ocr_text, encoding="utf-8")
        logger.info("Wrote ocr.txt — %d chars", len(ocr_text))
    elif skip_ocr:
        logger.info("--skip-ocr set — skipping Vision API call")

    final_parts = [e.text for e in text_elements if e.text]
    if ocr_text:
        final_parts.append(ocr_text)
    final_content = "\n\n".join(final_parts)
    (target_dir / "final-content.txt").write_text(final_content, encoding="utf-8")

    meta = {
        "page_db_id": resolved.page_db_id,
        "page_title": resolved.page_title,
        "page_onenote_id": resolved.page_onenote_id,
        "section": resolved.section_name,
        "notebook": resolved.notebook_name,
        "text_element_count": len(text_elements),
        "image_element_count": len(image_elements),
        "images_fetched": len(image_bytes_map),
        "has_handwriting": page_content.has_handwriting,
        "ink_stroke_count": len(page_content.ink_strokes),
        "composite_size": list(composite_size) if composite_size else None,
        "composite_png_bytes": len(composite_bytes) if composite_bytes else 0,
        "typed_text_chars": len(typed_text),
        "ocr_chars": len(ocr_text),
        "final_content_chars": len(final_content),
        "skipped_ocr": skip_ocr,
    }
    (target_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    logger.info("Done.")
    if ocr_text:
        preview = ocr_text[:500].replace("\n", " ")
        logger.info("OCR preview (first 500 chars): %s%s", preview, "..." if len(ocr_text) > 500 else "")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test the composite + OCR pipeline on a single page.")
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--page-id", type=int, metavar="ID", help="Page DB id")
    selector.add_argument("--page-title", type=str, metavar="TITLE", help="Page title (case-insensitive match)")
    parser.add_argument("--notebook", type=str, default=None, help="Notebook name or DB id (disambiguates --page-title)")
    parser.add_argument("--output-dir", type=Path, default=Path("ocr-test-output"), help="Output root dir (default: ./ocr-test-output)")
    parser.add_argument("--skip-ocr", action="store_true", help="Build the composite but do not call Vision")
    args = parser.parse_args()

    asyncio.run(main(
        page_id=args.page_id,
        page_title=args.page_title,
        notebook=args.notebook,
        output_dir=args.output_dir,
        skip_ocr=args.skip_ocr,
    ))
