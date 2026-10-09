"""Cold production OCR module на собственном изображении без предварительного SDK import."""

import json
import subprocess
import sys
from enum import IntEnum
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw

from memes_worker.infrastructure.ml.images import main as images_main


class OCRSmokeLimit(IntEnum):
    """Небольшие фиксированные ресурсы синтетического входа."""

    WIDTH = 800
    HEIGHT = 400
    FONT_SIZE = 64
    TEXT_LEFT = 60
    TEXT_TOP = 140
    TIMEOUT_SECONDS = 180


def main() -> None:
    """Новый интерпретатор проверяет настоящий порядок загрузки zlib и Paddle."""
    with TemporaryDirectory(prefix="work-", dir="/var/cache/memes-worker") as raw:
        directory = Path(raw)
        image = Image.new("RGB", (OCRSmokeLimit.WIDTH, OCRSmokeLimit.HEIGHT), "white")
        ImageDraw.Draw(image).text(
            (OCRSmokeLimit.TEXT_LEFT, OCRSmokeLimit.TEXT_TOP),
            "MEME 123",
            fill="black",
            font_size=OCRSmokeLimit.FONT_SIZE,
        )
        source = directory / "source"
        image.save(source, format="PNG")
        request = directory / "request.json"
        result = directory / "response.json"
        request.write_text(
            json.dumps({"source": str(source), "directory": str(directory), "operation": "frames"}),
            encoding="utf-8",
        )
        sys.argv = ["images", str(request), str(result)]
        images_main()
        frames = json.loads(result.read_text(encoding="utf-8"))["frames"]
        request.write_text(
            json.dumps(
                {
                    "inputs": frames,
                    "ocr_detection": "/models/ocr/detection",
                    "ocr_ru": "/models/ocr/ru",
                    "ocr_en": "/models/ocr/en",
                    "ocr_use_gpu": False,
                }
            ),
            encoding="utf-8",
        )
        subprocess.run(  # noqa: S603 -- Фиксированный module и собственный синтетический вход.
            [
                sys.executable,
                "-m",
                "memes_worker.infrastructure.ml.ocr",
                str(request),
                str(result),
            ],
            cwd=directory,
            check=True,
            timeout=OCRSmokeLimit.TIMEOUT_SECONDS,
        )
        fragments = json.loads(result.read_text(encoding="utf-8"))["fragments"]
        sys.stdout.write(
            "SMOKE:"
            + json.dumps(
                {
                    "fragments": len(fragments),
                    "languages": sorted({x["language"] for x in fragments}),
                }
            )
            + "\n"
        )


if __name__ == "__main__":
    main()
