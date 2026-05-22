import asyncio
from functools import lru_cache

from google.api_core.client_options import ClientOptions
from google.cloud import vision

from app.core.config import settings


class OCRClient:
    def __init__(self) -> None:
        self._client = vision.ImageAnnotatorClient(
            client_options=ClientOptions(api_key=settings.GOOGLE_CLOUD_VISION_API_KEY)
        )

    def run_ocr(self, image_bytes: bytes) -> str:
        """Run DOCUMENT_TEXT_DETECTION on a composite page image (slides + ink overlay)."""
        image = vision.Image(content=image_bytes)
        response = self._client.document_text_detection(image=image)  # type: ignore[attr-defined]
        if response.error.message:
            raise RuntimeError(f"Vision API error: {response.error.message}")
        return response.full_text_annotation.text.strip()

    async def run_ocr_tiles(self, tile_bytes: list[bytes]) -> list[str]:
        """OCR multiple tiles in parallel. Vision SDK is sync — we wrap in threads.

        Returns one string per input tile, in the same order. Failures bubble up.
        """
        if not tile_bytes:
            return []
        if len(tile_bytes) == 1:
            return [self.run_ocr(tile_bytes[0])]
        return await asyncio.gather(*(asyncio.to_thread(self.run_ocr, b) for b in tile_bytes))


@lru_cache
def get_ocr_client() -> OCRClient:
    return OCRClient()
