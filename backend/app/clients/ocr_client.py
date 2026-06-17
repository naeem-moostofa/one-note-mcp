import asyncio
from functools import lru_cache

from google.api_core.client_options import ClientOptions
from google.cloud import vision

from app.core.config import settings

_VISION_TIMEOUT_SECONDS = 60


class OCRClient:
    def __init__(self) -> None:
        self._client = vision.ImageAnnotatorClient(
            client_options=ClientOptions(api_key=settings.GOOGLE_CLOUD_VISION_API_KEY)
        )
        # OCRClient is a process singleton (get_ocr_client is lru_cached), so this instance
        # semaphore is process-wide — matching Google Vision's per-project quota.
        self._semaphore = asyncio.Semaphore(settings.SYNC_VISION_CONCURRENCY)

    async def run_ocr_async(self, image_bytes: bytes) -> str:
        """Async wrapper: cap concurrent OCR calls and offload the blocking gRPC call.

        The semaphore is acquired on the event loop (asyncio.Semaphore is not thread-safe);
        only the blocking run_ocr runs in a worker thread."""
        async with self._semaphore:
            return await asyncio.to_thread(self.run_ocr, image_bytes)

    def run_ocr(self, image_bytes: bytes) -> str:
        """Run DOCUMENT_TEXT_DETECTION on a composite page image (slides + ink overlay)."""
        image = vision.Image(content=image_bytes)
        response = self._client.document_text_detection(  # type: ignore[attr-defined]
            image=image,
            timeout=_VISION_TIMEOUT_SECONDS,
        )
        if response.error.message:
            raise RuntimeError(f"Vision API error: {response.error.message}")
        return response.full_text_annotation.text.strip()


@lru_cache
def get_ocr_client() -> OCRClient:
    return OCRClient()
