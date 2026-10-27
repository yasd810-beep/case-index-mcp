from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models" / "rapidocr"


@dataclass
class OCRResult:
    text: str
    confidence: float
    engine: str
    needs_review: bool
    reasons: list[str]
    fallback_text: str | None = None
    fallback_confidence: float | None = None
    fallback_engine: str | None = None
    fallback_error: str | None = None


def verify_models() -> None:
    manifest = ROOT / "models" / "SHA256.json"
    expected = json.loads(manifest.read_text(encoding="utf-8"))
    for relative, wanted in expected.items():
        path = ROOT / relative
        if not path.is_file():
            raise RuntimeError(f"Модель отсутствует: {relative}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != wanted:
            raise RuntimeError(f"Контрольная сумма модели не совпала: {relative}")


class OCRProcessor:
    """RapidOCR/OpenVINO CPU with an explicit cyrillic PaddleOCR second pass."""

    def __init__(self, confidence_threshold: float = 60.0, min_characters: int = 40):
        self.confidence_threshold = confidence_threshold
        self.min_characters = min_characters
        self._rapid = None
        self._paddle = None
        verify_models()

    def _get_rapid(self):
        if self._rapid is None:
            import openvino
            sys.modules.setdefault("openvino.runtime", openvino)
            from rapidocr import RapidOCR

            params = {
                "Rec.model_path": str(MODELS / "cyrillic_v5_rec_mobile.onnx"),
                "Rec.rec_keys_path": str(MODELS / "ppocrv5_cyrillic_dict.txt"),
                "Rec.rec_img_shape": [3, 48, 320],
                "Det.model_path": str(MODELS / "ppocr_v5_det_mobile.onnx"),
                "Global.with_onnx": False,
                "Global.with_openvino": True,
            }
            with redirect_stdout(sys.stderr):
                self._rapid = RapidOCR(params=params)
        return self._rapid

    def _get_paddle(self):
        if self._paddle is None:
            from paddleocr import PaddleOCR

            with redirect_stdout(sys.stderr):
                self._paddle = PaddleOCR(
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    text_detection_model_name="PP-OCRv5_mobile_det",
                    text_recognition_model_name="cyrillic_PP-OCRv5_mobile_rec",
                    enable_mkldnn=True,
                )
        return self._paddle

    @staticmethod
    def _rapid_run(engine, image: Path) -> tuple[str, float]:
        with redirect_stdout(sys.stderr):
            output = engine(str(image))
        texts = list(output.txts or [])
        scores = [float(value) for value in (output.scores or [])]
        confidence = statistics.mean(scores) * 100 if scores else 0.0
        return "\n".join(texts).strip() + "\n", confidence

    @staticmethod
    def _paddle_run(engine, image: Path) -> tuple[str, float]:
        texts: list[str] = []
        scores: list[float] = []
        with redirect_stdout(sys.stderr):
            results = engine.predict(input=str(image))
            for result in results:
                data = result if isinstance(result, dict) else getattr(result, "json", None)
                if callable(data):
                    data = data()
                if data is None:
                    continue
                data = data.get("res", data)
                texts.extend(data.get("rec_texts", []) or [])
                scores.extend(float(value) for value in (data.get("rec_scores", []) or []))
        confidence = statistics.mean(scores) * 100 if scores else 0.0
        return "\n".join(texts).strip() + "\n", confidence

    def recognize(self, image: Path, use_fallback: bool = True) -> OCRResult:
        primary_error = None
        try:
            text, confidence = self._rapid_run(self._get_rapid(), image)
            engine = "rapidocr-openvino-cpu"
        except Exception as exc:
            text, confidence, engine = "", 0.0, "none"
            primary_error = f"{type(exc).__name__}: {exc}"

        reasons: list[str] = []
        if primary_error:
            reasons.append("primary_error")
        if confidence < self.confidence_threshold:
            reasons.append("low_confidence")
        if len("".join(text.split())) < self.min_characters:
            reasons.append("little_text")
        if "\ufffd" in text:
            reasons.append("replacement_characters")

        result = OCRResult(text, round(confidence, 3), engine, bool(reasons), reasons)
        if not reasons or not use_fallback:
            if primary_error:
                raise RuntimeError(primary_error)
            return result

        try:
            fallback_text, fallback_confidence = self._paddle_run(self._get_paddle(), image)
            result.fallback_text = fallback_text
            result.fallback_confidence = round(fallback_confidence, 3)
            result.fallback_engine = "paddleocr-cyrillic-cpu"
            # A second pass replaces the primary only when it is at least as complete
            # and measurably more confident. Both outputs are retained either way.
            if (
                fallback_confidence >= confidence + 3
                and len("".join(fallback_text.split())) >= len("".join(text.split())) * 0.8
            ) or primary_error:
                result.text = fallback_text
                result.confidence = round(fallback_confidence, 3)
                result.engine = result.fallback_engine
        except Exception as exc:
            result.fallback_error = f"{type(exc).__name__}: {exc}"
        return result
