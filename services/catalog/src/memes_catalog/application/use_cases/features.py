"""Проверка собственного межконтекстного контракта признаков."""

from enum import IntEnum, StrEnum
from math import isfinite
from typing import cast
from uuid import UUID

from memes_catalog.application.dto.catalog import FeatureUpdate
from memes_kernel.values import FileRef, Number, Sha256
from memes_platform.application.dto.json import JsonValue
from memes_platform.application.errors import ApplicationError, ErrorCode


class FeatureKind(StrEnum):
    """Семантические способы поиска, известные Catalog."""

    PREVIEW = "preview"
    OCR = "ocr"
    TRANSCRIPT = "transcript"
    VISUAL_EMBEDDING = "visual_embedding"
    AUDIO_EMBEDDING = "audio_embedding"
    TEXT_EMBEDDING = "text_embedding"


class FeatureState(StrEnum):
    """Состояния публичной проекции признака."""

    PENDING = "pending"
    AVAILABLE = "available"
    EMPTY = "empty"
    NOT_APPLICABLE = "not_applicable"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ContractLimit(IntEnum):
    """Ограничение размера пакета результатов."""

    FRAGMENTS = 2000
    DIMENSIONS = 8192
    DURATION_MS = 300000
    PREVIEW_BYTES = 10485760
    TEXT_CHARACTERS = 65536


def _span(data: JsonValue) -> None:
    """Проверить временной интервал извлечённого фрагмента."""
    if data is None:
        return
    if not isinstance(data, dict):
        raise ApplicationError(ErrorCode.INVALID_INPUT)
    start, end = data.get("start_ms"), data.get("end_ms")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < Number.ZERO
        or end <= start
        or end > ContractLimit.DURATION_MS
    ):
        raise ApplicationError(ErrorCode.INVALID_INPUT)


def validate_feature(update: FeatureUpdate) -> None:
    """Проверить результаты перед транзакционной приёмкой.

    Args:
        update: Межконтекстный DTO.
    """
    try:
        kind, state = FeatureKind(update.kind), FeatureState(update.state)
    except ValueError as error:
        raise ApplicationError(ErrorCode.INVALID_INPUT) from error
    if min(update.source_revision, update.generation) < Number.ONE:
        raise ApplicationError(ErrorCode.INVALID_INPUT)
    version = update.feature_version
    required = ("model", "weights", "task_adapter", "preprocessing")
    if any(not isinstance(version.get(field), str) or not version[field] for field in required):
        raise ApplicationError(ErrorCode.INVALID_INPUT)
    if state not in (FeatureState.AVAILABLE, FeatureState.EMPTY):
        if update.result is not None:
            raise ApplicationError(ErrorCode.INVALID_INPUT)
        return
    if update.result is None:
        raise ApplicationError(ErrorCode.INVALID_INPUT)
    if kind is FeatureKind.PREVIEW:
        key = update.result.get("object_key")
        if not isinstance(key, str) or not key.startswith(f"attempts/{update.media_id}/"):
            raise ApplicationError(ErrorCode.INVALID_INPUT)
        file = update.result.get("file")
        if (
            not isinstance(file, dict)
            or not isinstance(file.get("size_bytes"), int)
            or isinstance(file.get("size_bytes"), bool)
            or not Number.ZERO < cast(int, file["size_bytes"]) <= ContractLimit.PREVIEW_BYTES
            or state is FeatureState.EMPTY
        ):
            raise ApplicationError(ErrorCode.INVALID_INPUT)
        try:
            FileRef(
                id=UUID(str(file.get("id"))),
                sha256=Sha256(value=str(file.get("sha256"))),
                size_bytes=cast(int, file["size_bytes"]),
            )
        except ValueError as error:
            raise ApplicationError(ErrorCode.INVALID_INPUT) from error
        return
    text = kind in (FeatureKind.OCR, FeatureKind.TRANSCRIPT)
    items = update.result.get("fragments" if text else "items")
    if not isinstance(items, list) or not Number.ZERO <= len(items) <= ContractLimit.FRAGMENTS:
        raise ApplicationError(ErrorCode.INVALID_INPUT)
    if (state is FeatureState.EMPTY) != (len(items) == Number.ZERO):
        raise ApplicationError(ErrorCode.INVALID_INPUT)
    for item in items:
        if not isinstance(item, dict):
            raise ApplicationError(ErrorCode.INVALID_INPUT)
        _span(item.get("span"))
        if text:
            if (
                not isinstance(item.get("text"), str)
                or not item["text"]
                or len(cast(str, item["text"])) > ContractLimit.TEXT_CHARACTERS
                or item.get("language") not in ("ru", "en")
            ):
                raise ApplicationError(ErrorCode.INVALID_INPUT)
        else:
            dimensions = version.get("dimensions")
            values = item.get("values")
            if (
                not isinstance(dimensions, int)
                or not Number.ZERO < dimensions <= ContractLimit.DIMENSIONS
            ):
                raise ApplicationError(ErrorCode.INVALID_INPUT)
            if not isinstance(values, list) or len(values) != dimensions:
                raise ApplicationError(ErrorCode.INVALID_INPUT)
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                for value in values
            ):
                raise ApplicationError(ErrorCode.INVALID_INPUT)
            if not any(value != Number.ZERO for value in values):
                raise ApplicationError(ErrorCode.INVALID_INPUT)
