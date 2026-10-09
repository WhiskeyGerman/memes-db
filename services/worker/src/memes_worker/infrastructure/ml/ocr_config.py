"""Локальный контракт PaddleOCR без неявного скачивания classifier."""

from enum import StrEnum
from pathlib import Path

from memes_worker.infrastructure.ml.diagnostics import (
    DiagnosticMessage,
    PluginModelConfigurationError,
)


class OCRArtifact(StrEnum):
    """Обязательные файлы официального inference артефакта."""

    MODEL = "inference.pdmodel"
    PARAMETERS = "inference.pdiparams"


def validate_ocr_directory(directory: Path) -> None:
    """Потребовать оба непустых inference файла до принятия задач."""
    try:
        for name in OCRArtifact:
            target = directory / name
            if not target.is_file() or target.stat().st_size <= 0:
                raise PluginModelConfigurationError(DiagnosticMessage.INVALID_LOCAL_MODEL_ARTIFACT)
    except OSError as error:
        raise PluginModelConfigurationError(
            DiagnosticMessage.INVALID_LOCAL_MODEL_ARTIFACT
        ) from error


def ocr_arguments(
    *, language: str, detection: Path, recognition: Path, use_gpu: bool
) -> dict[str, object]:
    """Указать classifier directory, хотя угол классифицировать не требуется.

    PaddleOCR 2.10 вызывает maybe_download для classifier даже при
    use_angle_cls=False. Проверенный detector содержит оба файла inference,
    поэтому downloader завершится без обращения к сети. Отключённый classifier
    не открывает эти веса и не выполняет inference.

    Args:
        language: Язык локального распознавателя.
        detection: Проверенная директория детектора.
        recognition: Проверенная директория распознавателя.
        use_gpu: Использовать GPU в совместимой сборке Paddle.
    """
    validate_ocr_directory(detection)
    validate_ocr_directory(recognition)
    return {
        "lang": language,
        "use_angle_cls": False,
        "show_log": False,
        "det_model_dir": str(detection),
        "rec_model_dir": str(recognition),
        "cls_model_dir": str(detection),
        "use_gpu": use_gpu,
    }
