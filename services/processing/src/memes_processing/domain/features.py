"""Извлечённое содержимое и совместимость поисковых признаков."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import NewType, TypeAlias
from uuid import UUID

from memes_kernel.entity import Entity
from memes_kernel.errors import DomainError
from memes_kernel.values import FileRef, Number, require_aware, require_identifier, require_ordered

ExtractionId = NewType("ExtractionId", UUID)
CycleId = NewType("CycleId", UUID)


class FeatureKind(StrEnum):
    """Независимые способы извлечения содержимого."""

    PREVIEW = "preview"
    OCR = "ocr"
    TRANSCRIPT = "transcript"
    VISUAL_EMBEDDING = "visual_embedding"
    AUDIO_EMBEDDING = "audio_embedding"
    TEXT_EMBEDDING = "text_embedding"


class Language(StrEnum):
    """Поддерживаемые языки извлечённого текста."""

    RUSSIAN = "ru"
    ENGLISH = "en"


class FeatureState(StrEnum):
    """Доступность результата независимо от очереди и индекса."""

    PENDING = "pending"
    AVAILABLE = "available"
    EMPTY = "empty"
    NOT_APPLICABLE = "not_applicable"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FeatureViolation(StrEnum):
    """Нарушения правил извлечённых признаков."""

    INVALID_VERSION = "invalid_feature_version"
    INVALID_VECTOR = "invalid_vector"
    INVALID_FRAGMENT = "invalid_fragment"
    INVALID_STATE = "invalid_feature_state"
    WRONG_RESULT = "wrong_feature_result"
    STALE_CYCLE = "stale_processing_cycle"
    CLOSED_CYCLE = "closed_processing_cycle"


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureVersion:
    """Полная версия способа получения и интерпретации признака.

    Attributes:
        model: Идентификатор модели.
        weights: Ревизия весов.
        task_adapter: Версия адаптера задачи.
        preprocessing: Версия предобработки.
        space: Пространство сравнения, общее для совместимых модальностей.
        dimensions: Размерность вектора либо :data:`None` для иных результатов.
    """

    model: str
    weights: str
    task_adapter: str
    preprocessing: str
    space: str | None = None
    dimensions: int | None = None

    def __post_init__(self) -> None:
        """Запретить неполную версию пространства."""
        if not all(
            value.strip()
            for value in (self.model, self.weights, self.task_adapter, self.preprocessing)
        ):
            raise DomainError(FeatureViolation.INVALID_VERSION)
        if (self.space is None) != (self.dimensions is None):
            raise DomainError(FeatureViolation.INVALID_VERSION)
        if self.dimensions is not None and self.dimensions < Number.ONE:
            raise DomainError(FeatureViolation.INVALID_VERSION)
        if self.space is not None and not self.space.strip():
            raise DomainError(FeatureViolation.INVALID_VERSION)

    def compatible_with(self, other: "FeatureVersion") -> bool:
        """Проверить пространство сравнения двух модальностей.

        Args:
            other: Версия другого признака.

        Returns:
            bool: Совпадают ли объявленное пространство и размерность.
        """
        return (
            self.space is not None
            and self.space == other.space
            and self.dimensions == other.dimensions
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TimeSpan:
    """Полуоткрытый интервал фрагмента относительно начала оригинала."""

    start: timedelta
    end: timedelta

    def __post_init__(self) -> None:
        """Проверить порядок временных границ."""
        if self.start < timedelta() or self.end <= self.start:
            raise DomainError(FeatureViolation.INVALID_FRAGMENT)


@dataclass(frozen=True, slots=True, kw_only=True)
class TextFragment:
    """Текст изображения или фрагмента речи."""

    text: str
    language: Language
    span: TimeSpan | None = None

    def __post_init__(self) -> None:
        """Запретить пустой фрагмент при наличии результата."""
        if not self.text.strip():
            raise DomainError(FeatureViolation.INVALID_FRAGMENT)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractedText:
    """Упорядоченные фрагменты распознанного содержимого."""

    fragments: tuple[TextFragment, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Embedding:
    """Вектор с версией пространства и необязательным интервалом."""

    version: FeatureVersion
    values: tuple[float, ...]
    span: TimeSpan | None = None

    def __post_init__(self) -> None:
        """Проверить размерность, конечность и ненулевой вектор."""
        if (
            self.version.dimensions != len(self.values)
            or not self.values
            or not all(isfinite(value) for value in self.values)
            or not any(value != Number.ZERO for value in self.values)
        ):
            raise DomainError(FeatureViolation.INVALID_VECTOR)


@dataclass(frozen=True, slots=True, kw_only=True)
class Embeddings:
    """Набор векторов отдельных фрагментов одного этапа."""

    items: tuple[Embedding, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class Preview:
    """Производный файл для показа карточки."""

    file: FileRef


FeatureResult: TypeAlias = ExtractedText | Embeddings | Preview


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class FeatureExtraction(Entity[ExtractionId]):
    """Один независимый цикл получения признака медиа.

    Новый цикл создаётся отдельной сущностью. История старого сохраняется;
    результат с другим :class:`CycleId` не может перезаписать текущий.
    """

    media_id: UUID
    source: FileRef
    cycle_id: CycleId
    kind: FeatureKind
    version: FeatureVersion
    requested_at: datetime
    state: FeatureState = FeatureState.PENDING
    result: FeatureResult | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        """Проверить соответствие состояния содержимому."""
        Entity.__post_init__(self)
        require_identifier(self.media_id)
        require_identifier(self.cycle_id)
        require_aware(self.requested_at)
        vector_kinds = {
            FeatureKind.VISUAL_EMBEDDING,
            FeatureKind.AUDIO_EMBEDDING,
            FeatureKind.TEXT_EMBEDDING,
        }
        if self.kind in vector_kinds and self.version.dimensions is None:
            raise DomainError(FeatureViolation.INVALID_VERSION)
        if (self.state is FeatureState.PENDING) != (self.finished_at is None):
            raise DomainError(FeatureViolation.INVALID_STATE)
        if (self.state in {FeatureState.AVAILABLE, FeatureState.EMPTY}) != (
            self.result is not None
        ):
            raise DomainError(FeatureViolation.INVALID_STATE)
        if self.finished_at is not None:
            require_ordered(self.finished_at, self.requested_at)
        if self.result is not None:
            self._validate_result(self.result)
            empty = isinstance(self.result, ExtractedText) and not self.result.fragments
            empty = empty or (isinstance(self.result, Embeddings) and not self.result.items)
            if empty != (self.state is FeatureState.EMPTY):
                raise DomainError(FeatureViolation.INVALID_STATE)

    def complete(
        self, *, cycle_id: CycleId, result: FeatureResult, now: datetime
    ) -> "FeatureExtraction":
        """Принять содержимое действующего цикла.

        Args:
            cycle_id: Цикл полученного результата.
            result: Проверенное содержимое.
            now: Серверное время приёмки.

        Returns:
            FeatureExtraction: Завершённое состояние.
        """
        self._require_pending(cycle_id)
        self._validate_result(result)
        empty = isinstance(result, ExtractedText) and not result.fragments
        empty = empty or (isinstance(result, Embeddings) and not result.items)
        return replace(
            self,
            state=FeatureState.EMPTY if empty else FeatureState.AVAILABLE,
            result=result,
            finished_at=now,
            revision=self.revision.next(),
        )

    def finish_without_result(
        self, *, cycle_id: CycleId, state: FeatureState, now: datetime
    ) -> "FeatureExtraction":
        """Завершить неприменимый, отменённый или неудачный этап.

        Args:
            cycle_id: Завершаемый цикл.
            state: Причина отсутствия результата.
            now: Серверное время.
        """
        self._require_pending(cycle_id)
        if state not in {FeatureState.NOT_APPLICABLE, FeatureState.FAILED, FeatureState.CANCELLED}:
            raise DomainError(FeatureViolation.INVALID_STATE)
        return replace(self, state=state, finished_at=now, revision=self.revision.next())

    def _require_pending(self, cycle_id: CycleId) -> None:
        if cycle_id != self.cycle_id:
            raise DomainError(FeatureViolation.STALE_CYCLE)
        if self.state is not FeatureState.PENDING:
            raise DomainError(FeatureViolation.CLOSED_CYCLE)

    def _validate_result(self, result: FeatureResult) -> None:
        valid = False
        if self.kind is FeatureKind.PREVIEW:
            valid = isinstance(result, Preview)
        elif self.kind is FeatureKind.TRANSCRIPT:
            valid = isinstance(result, ExtractedText) and all(
                item.span is not None for item in result.fragments
            )
        elif self.kind is FeatureKind.OCR:
            valid = isinstance(result, ExtractedText)
        elif isinstance(result, Embeddings):
            valid = all(item.version == self.version for item in result.items)
        if not valid:
            raise DomainError(FeatureViolation.WRONG_RESULT)
