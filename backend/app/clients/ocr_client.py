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


@lru_cache
def get_ocr_client() -> OCRClient:
    return OCRClient()
