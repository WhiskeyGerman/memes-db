"""Одноразовая стадия Build скачивает строго выбранную ревизию весов."""

import argparse
import asyncio
import sys
from enum import IntEnum, StrEnum
from pathlib import Path

from memes_worker.infrastructure.ml.download_ocr import OCRSource, prepare_ocr
from memes_worker.infrastructure.ml.manifest import write_manifest
from memes_worker.infrastructure.ml.model_config import EmbeddingCapacity
from memes_worker.infrastructure.ml.ocr_config import validate_ocr_directory
from memes_worker.infrastructure.ml.process import execute


class BuildLimit(IntEnum):
    """Сборка весов имеет отдельный ограниченный бюджет."""

    DOWNLOAD_SECONDS = 1800


class PreparedModel(StrEnum):
    """Известные источники модели."""

    JINA = "jina"
    ASR = "asr"
    OCR = "ocr"
    ALL = "all"


async def prepare(model: PreparedModel, directory: Path, *, dimensions: int) -> None:
    """Подготовить один артефакт или полный набор весов для LAN-воркера.

    Args:
        model: Модель или весь набор.
        directory: Каталог результирующего артефакта.
        dimensions: Допустимая размерность эмбеддингов.
    """
    if model is PreparedModel.ALL:
        for selected in (PreparedModel.JINA, PreparedModel.ASR):
            await prepare(selected, directory / selected, dimensions=dimensions)
        prepare_ocr(directory / PreparedModel.OCR)
    elif model is PreparedModel.OCR:
        validate_ocr_directory(directory)
        write_manifest(directory, model=OCRSource.MODEL, revision=OCRSource.REVISION)
    else:
        await execute(
            [
                sys.executable,
                "-m",
                "memes_worker.infrastructure.ml.download_models",
                "--model",
                model,
                "--directory",
                str(directory),
                "--dimensions",
                str(dimensions),
            ],
            timeout_seconds=BuildLimit.DOWNLOAD_SECONDS,
            directory=Path.cwd(),
        )


def main() -> None:
    """Не запускать скачивание модели внутри процесса приема задач."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, choices=list(PreparedModel))
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--dimensions", type=int, default=EmbeddingCapacity.DIMENSIONS)
    arguments = parser.parse_args()
    asyncio.run(
        prepare(
            PreparedModel(arguments.model), arguments.directory, dimensions=arguments.dimensions
        )
    )


if __name__ == "__main__":
    main()
