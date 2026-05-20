import io
from functools import lru_cache

from PIL import Image
from surya.detection import DetectionPredictor
from surya.foundation import FoundationPredictor
from surya.recognition import RecognitionPredictor


class OCRClient:
    def __init__(self) -> None:
        foundation = FoundationPredictor()
        self._rec_predictor = RecognitionPredictor(foundation)
        self._det_predictor = DetectionPredictor()

    def run_ocr(self, image_bytes: bytes) -> str:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        results = self._rec_predictor([image], det_predictor=self._det_predictor)
        if not results or not results[0].text_lines:
            return ""
        return "\n".join(line.text for line in results[0].text_lines if line.text)


@lru_cache
def get_ocr_client() -> OCRClient:
    return OCRClient()
