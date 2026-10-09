"""Преобразование прикладных сообщений в проверенные доменные признаки."""

from datetime import timedelta
from enum import IntEnum
from uuid import UUID

from memes_kernel.values import FileRef, Sha256
from memes_processing.application.dto.coordinator import (
    CoordinatorCode,
    CoordinatorError,
    JSONValue,
    Payload,
)
from memes_processing.domain.features import (
    Embedding,
    Embeddings,
    ExtractedText,
    FeatureKind,
    FeatureResult,
    FeatureVersion,
    Language,
    Preview,
    TextFragment,
    TimeSpan,
)


class ResultLimit(IntEnum):
    """Ограничения количества элементов ответа модели."""

    FRAGMENTS = 20000
    VECTORS = 600
    TEXT_CHARACTERS = 1000000
    MILLISECONDS = 1000
    DURATION_MS = 300000


def mapping(value: JSONValue) -> Payload:
    """Проверить объект прикладного сообщения."""
    if not isinstance(value, dict):
        raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
    return value


def sequence(value: JSONValue) -> list[JSONValue]:
    """Проверить массив прикладного сообщения."""
    if not isinstance(value, list):
        raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
    return value


def string(value: JSONValue) -> str:
    """Проверить строку прикладного сообщения."""
    if not isinstance(value, str):
        raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
    return value


def integer(value: JSONValue) -> int:
    """Проверить целое число, исключая логические значения."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
    return value


def version_payload(version: FeatureVersion) -> Payload:
    """Подготовить полный паспорт модели для межсервисного контракта."""
    return {
        "model": version.model,
        "weights": version.weights,
        "task_adapter": version.task_adapter,
        "preprocessing": version.preprocessing,
        "space": version.space,
        "dimensions": version.dimensions,
    }


def parse_version(payload: Payload) -> FeatureVersion:
    """Восстановить проверенную версию модели."""
    space = payload.get("space")
    dimensions = payload.get("dimensions")
    return FeatureVersion(
        model=string(payload.get("model")),
        weights=string(payload.get("weights")),
        task_adapter=string(payload.get("task_adapter")),
        preprocessing=string(payload.get("preprocessing")),
        space=None if space is None else string(space),
        dimensions=None if dimensions is None else integer(dimensions),
    )


def parse_span(value: JSONValue) -> TimeSpan | None:
    """Проверить временной интервал результата."""
    if value is None:
        return None
    span = mapping(value)
    if integer(span.get("end_ms")) > ResultLimit.DURATION_MS:
        raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
    return TimeSpan(
        start=timedelta(milliseconds=integer(span.get("start_ms"))),
        end=timedelta(milliseconds=integer(span.get("end_ms"))),
    )


def parse_result(kind: FeatureKind, version: FeatureVersion, payload: Payload) -> FeatureResult:
    """Проверить тип, размерность и содержимое результата модели."""
    if kind is FeatureKind.PREVIEW:
        file = mapping(payload.get("file"))
        return Preview(
            file=FileRef(
                id=UUID(string(file.get("id"))),
                sha256=Sha256(value=string(file.get("sha256"))),
                size_bytes=integer(file.get("size_bytes")),
            )
        )
    if kind in {FeatureKind.OCR, FeatureKind.TRANSCRIPT}:
        fragments = sequence(payload.get("fragments"))
        if len(fragments) > ResultLimit.FRAGMENTS:
            raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
        texts = tuple(
            TextFragment(
                text=string(mapping(fragment).get("text")),
                language=Language(string(mapping(fragment).get("language"))),
                span=parse_span(mapping(fragment).get("span")),
            )
            for fragment in fragments
        )
        if sum(len(fragment.text) for fragment in texts) > ResultLimit.TEXT_CHARACTERS:
            raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
        return ExtractedText(fragments=texts)
    vectors = sequence(payload.get("items"))
    if len(vectors) > ResultLimit.VECTORS:
        raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
    items: list[Embedding] = []
    for vector in vectors:
        raw = mapping(vector)
        values = sequence(raw.get("values"))
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in values
        ):
            raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
        numeric: tuple[float, ...] = tuple(
            float(value) for value in values if isinstance(value, (int, float))
        )
        items.append(Embedding(version=version, values=numeric, span=parse_span(raw.get("span"))))
    return Embeddings(items=tuple(items))
