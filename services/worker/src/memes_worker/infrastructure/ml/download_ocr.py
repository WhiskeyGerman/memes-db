"""Build-адаптер скачивания официальных inference артефактов OCR."""

import shutil
import tarfile
from enum import IntEnum, StrEnum
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

import httpx

from memes_worker.infrastructure.ml.manifest import ManifestName, verify_manifest, write_manifest
from memes_worker.infrastructure.ml.ocr_config import OCRArtifact, validate_ocr_directory


class OCRSource(StrEnum):
    """Официальные версии, совместимые с паспортом воркера."""

    MODEL = "PaddleOCR"
    REVISION = "PP-OCRv4-ru-en@2.10.0"
    DETECTION = "https://paddleocr.bj.bcebos.com/PP-OCRv4/chinese/ch_PP-OCRv4_det_infer.tar"
    RU = "https://paddleocr.bj.bcebos.com/PP-OCRv3/multilingual/cyrillic_PP-OCRv3_rec_infer.tar"
    EN = "https://paddleocr.bj.bcebos.com/PP-OCRv4/english/en_PP-OCRv4_rec_infer.tar"


class OCRDownloadLimit(IntEnum):
    """Ограничения памяти, размера и ожидания внешнего источника."""

    TIMEOUT_SECONDS = 60
    CHUNK_BYTES = 1048576
    MAXIMUM_BYTES = 536870912


def extract_inference(archive: Path, directory: Path) -> None:
    """Записать только обязательные обычные файлы без извлечения путей архива.

    Args:
        archive: Скачанный tar-архив.
        directory: Пустой временный каталог артефакта.
    """
    required = set(OCRArtifact)
    found: set[str] = set()
    with tarfile.open(archive) as source:
        for member in source:
            name = PurePosixPath(member.name).name
            if name not in required:
                continue
            if (
                not member.isfile()
                or name in found
                or not 0 < member.size <= OCRDownloadLimit.MAXIMUM_BYTES
            ):
                raise ValueError("invalid_ocr_archive")
            content = source.extractfile(member)
            if content is None:
                raise ValueError("invalid_ocr_archive")
            with content, (directory / name).open("wb") as output:
                shutil.copyfileobj(content, output, OCRDownloadLimit.CHUNK_BYTES)
            found.add(name)
    validate_ocr_directory(directory)


def prepare_ocr(directory: Path) -> None:
    """Подготовить три OCR-модели; готовые manifests проверить повторно.

    Args:
        directory: Общий каталог :file:`ocr` для detector и двух recognizers.
    """
    directory.mkdir(parents=True, exist_ok=True)
    sources = {"detection": OCRSource.DETECTION, "ru": OCRSource.RU, "en": OCRSource.EN}
    with httpx.Client(timeout=OCRDownloadLimit.TIMEOUT_SECONDS, follow_redirects=False) as client:
        for name, url in sources.items():
            destination = directory / name
            if (destination / ManifestName.FILE).exists():
                verify_manifest(destination, model=OCRSource.MODEL, revision=OCRSource.REVISION)
                validate_ocr_directory(destination)
                continue
            if destination.exists():
                raise ValueError(f"ocr_directory_without_manifest: {destination}")
            with TemporaryDirectory(dir=directory) as temporary:
                root = Path(temporary)
                archive = root / "model.tar"
                size = 0
                with client.stream("GET", url) as response, archive.open("wb") as output:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(OCRDownloadLimit.CHUNK_BYTES):
                        size += len(chunk)
                        if size > OCRDownloadLimit.MAXIMUM_BYTES:
                            raise ValueError("ocr_archive_too_large")
                        output.write(chunk)
                artifact = root / "artifact"
                artifact.mkdir()
                extract_inference(archive, artifact)
                write_manifest(artifact, model=OCRSource.MODEL, revision=OCRSource.REVISION)
                artifact.rename(destination)
