"""Изолированный PaddleOCR для русского и английского языков."""

import json
import sys
import zlib  # noqa: F401 -- Загрузить системный zlib до Paddle: иначе pyclipper повреждает поток.
from pathlib import Path

from paddleocr import PaddleOCR

from memes_worker.application.dto.runtime import JSONValue
from memes_worker.infrastructure.ml.diagnostics import DiagnosticMessage, PluginInputError
from memes_worker.infrastructure.ml.ocr_config import ocr_arguments
from memes_worker.infrastructure.ml.plugin_runtime import entrypoint
from memes_worker.infrastructure.wire import object_payload, value_string


def main() -> None:
    """Локальные детектор и распознаватели не скачиваются при Run."""
    request = object_payload(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    inputs = request.get("inputs")
    if not isinstance(inputs, list):
        raise PluginInputError(DiagnosticMessage.INVALID_MODEL_INPUT)
    fragments: list[JSONValue] = []
    for language, directory_key in (("ru", "ocr_ru"), ("en", "ocr_en")):
        recognizer = PaddleOCR(
            **ocr_arguments(
                language=language,
                detection=Path(value_string(request.get("ocr_detection"))),
                recognition=Path(value_string(request.get(directory_key))),
                use_gpu=request.get("ocr_use_gpu") is True,
            )
        )
        for raw in inputs:
            item = object_payload(raw)
            result = recognizer.ocr(value_string(item.get("path")), cls=False)
            for page in result or []:
                for line in page or []:
                    text = str(line[1][0]).strip()
                    if text:
                        fragments.append(
                            {"text": text, "language": language, "span": item.get("span")}
                        )
        del recognizer
    Path(sys.argv[2]).write_text(
        json.dumps({"fragments": fragments}, ensure_ascii=False), encoding="utf-8"
    )


if __name__ == "__main__":
    entrypoint(main)
