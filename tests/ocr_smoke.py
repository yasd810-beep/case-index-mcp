from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw

from case_indexer.ocr import OCRProcessor, verify_models


def main() -> None:
    verify_models()
    output = ROOT / "test-run" / "ocr-smoke"
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    image_path = output / "synthetic.png"
    image = Image.new("RGB", (1000, 180), "white")
    draw = ImageDraw.Draw(image)
    draw.text((30, 65), "SYNTHETIC OCR TEST 1234567890", fill="black", font_size=36)
    image.save(image_path)
    image.close()
    result = OCRProcessor().recognize(image_path, use_fallback=False)
    if result.engine != "rapidocr-openvino-cpu" or not result.text.strip():
        raise AssertionError(f"OCR smoke test failed: {result}")
    print("OK: model hashes and primary OCR; recognized length:", len(result.text.strip()))


if __name__ == "__main__":
    main()
