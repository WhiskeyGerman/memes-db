"""Строго версионированные контракты доставки результатов обработки."""

import json
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import AliasChoices, BaseModel, Field, model_validator

from memes_search.application.dto.projections import (
    EmbeddingVector,
    EmbeddingVersion,
    FeatureKind,
    FeatureProjection,
    FragmentLanguage,
    MediaStateChange,
    ProjectionState,
    TextFragment,
)
from memes_search.application.dto.queries import SearchLimit, SearchMediaKind
from memes_search.application.dto.samples import SampleFeatureChange, SampleState
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.domain.fragments import MatchedSpan


class SearchLanguage(StrEnum):
    """Языки индексируемого текста первой версии."""

    RUSSIAN = "ru"
    ENGLISH = "en"


class TimeSpanBody(BaseModel):
    """Интервал фрагмента относительно исходного файла."""

    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_interval(self) -> "TimeSpanBody":
        """Проверить строго возрастающие временные границы."""
        if self.end_ms <= self.start_ms:
            raise SearchError(SearchErrorCode.INVALID_FEATURE)
        return self


class TextFragmentBody(BaseModel):
    """Распознанный фрагмент на поддерживаемом языке."""

    text: str
    language: SearchLanguage
    span: TimeSpanBody | None = None


class TextResultBody(BaseModel):
    """Каноническая форма извлечённого текста."""

    fragments: list[TextFragmentBody] = Field(max_length=SearchLimit.MAXIMUM_FRAGMENTS)


class VectorBody(BaseModel):
    """Фрагментный вектор без собственной версии пространства."""

    values: list[float] = Field(min_length=1, max_length=SearchLimit.MAXIMUM_VECTOR_DIMENSIONS)
    span: TimeSpanBody | None = None


class EmbeddingResultBody(BaseModel):
    """Каноническая форма результата векторного этапа."""

    items: list[VectorBody] = Field(max_length=SearchLimit.MAXIMUM_FRAGMENTS)


class PreviewFileBody(BaseModel):
    """Ключ производного файла, который Search не хранит."""

    id: UUID
    sha256: str
    size_bytes: int


class PreviewResultBody(BaseModel):
    """Неиндексируемый производный артефакт."""

    file: PreviewFileBody
    object_key: str | None = None


class FeatureVersionBody(BaseModel):
    """Полная воспроизводимая версия model, preprocessing и пространства."""

    model: str = Field(min_length=1)
    weights: str = Field(min_length=1)
    task_adapter: str = Field(min_length=1)
    preprocessing: str = Field(min_length=1)
    space: str | None = None
    dimensions: int | None = Field(default=None, ge=1, le=SearchLimit.MAXIMUM_VECTOR_DIMENSIONS)

    def embedding_version(self) -> EmbeddingVersion:
        """Передать типизированную совместимость прикладному слою."""
        if self.space is None or self.dimensions is None:
            raise SearchError(SearchErrorCode.INVALID_FEATURE)
        return EmbeddingVersion(
            model=self.model,
            weights=self.weights,
            task_adapter=self.task_adapter,
            preprocessing=self.preprocessing,
            space=self.space,
            dimensions=self.dimensions,
        )

    def key(self) -> str:
        """Получить стабильный ключ поколения без лексического сравнения версий."""
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


class FeatureContentBody(BaseModel):
    """Общие поля результата публичного медиа или приватного образца."""

    event_id: UUID
    kind: FeatureKind
    cycle_id: UUID
    generation: int = Field(ge=1)
    state: ProjectionState
    feature_version: FeatureVersionBody
    result: TextResultBody | EmbeddingResultBody | PreviewResultBody | None

    def embedding_version(self) -> EmbeddingVersion | None:
        """Передать паспорт также pending-событий."""
        if self.kind in {
            FeatureKind.VISUAL_EMBEDDING,
            FeatureKind.AUDIO_EMBEDDING,
            FeatureKind.TEXT_EMBEDDING,
        }:
            return self.feature_version.embedding_version()
        return None

    def fragments(self) -> tuple[TextFragment, ...]:
        """Сохранить тексты, языки и интервалы без потерь."""
        if not isinstance(self.result, TextResultBody):
            return ()
        return tuple(
            TextFragment(
                text=item.text,
                language=FragmentLanguage(item.language),
                span=MatchedSpan(start_ms=item.span.start_ms, end_ms=item.span.end_ms)
                if item.span
                else None,
            )
            for item in self.result.fragments
        )

    def content(self) -> tuple[str, tuple[EmbeddingVector, ...]]:
        """Проверить совместимость вида результата и пространства."""
        if self.state not in {ProjectionState.AVAILABLE, ProjectionState.EMPTY}:
            if self.result is not None:
                raise SearchError(SearchErrorCode.INVALID_FEATURE)
            return "", ()
        if self.kind in {FeatureKind.OCR, FeatureKind.TRANSCRIPT}:
            if not isinstance(self.result, TextResultBody):
                raise SearchError(SearchErrorCode.INVALID_FEATURE)
            return " ".join(item.text for item in self.result.fragments), ()
        if self.kind is FeatureKind.PREVIEW:
            if not isinstance(self.result, PreviewResultBody):
                raise SearchError(SearchErrorCode.INVALID_FEATURE)
            return "", ()
        if not isinstance(self.result, EmbeddingResultBody) or self.feature_version.space is None:
            raise SearchError(SearchErrorCode.INVALID_FEATURE)
        vectors = tuple(
            EmbeddingVector(
                values=tuple(item.values),
                space_id=self.feature_version.space,
                version=self.feature_version.embedding_version(),
                span=MatchedSpan(start_ms=item.span.start_ms, end_ms=item.span.end_ms)
                if item.span
                else None,
            )
            for item in self.result.items
        )
        if any(len(item.values) != self.feature_version.dimensions for item in vectors):
            raise SearchError(SearchErrorCode.INVALID_FEATURE)
        return "", vectors


class FeatureDeliveryBody(FeatureContentBody):
    """Проекция публичного оригинала с авторитетной ревизией каталога."""

    media_id: UUID
    group_id: UUID
    group_revision: int = Field(ge=1)
    composition_revision: int | None = Field(default=None, ge=1)
    revision: int = Field(ge=1)
    published_at: datetime
    media_kind: SearchMediaKind

    def projection(self) -> FeatureProjection:
        """Преобразовать проверенное HTTP-событие в application DTO."""
        extracted_text, vectors = self.content()
        return FeatureProjection(
            event_id=self.event_id,
            media_id=self.media_id,
            group_id=self.group_id,
            group_revision=self.composition_revision or self.group_revision,
            revision=self.revision,
            published_at=self.published_at,
            media_kind=self.media_kind,
            kind=self.kind,
            cycle_id=self.cycle_id,
            generation=self.generation,
            state=self.state,
            version_key=self.feature_version.key(),
            embedding_version=self.embedding_version(),
            text=extracted_text,
            fragments=self.fragments(),
            vectors=vectors,
        )


class SampleFeatureDeliveryBody(FeatureContentBody):
    """Событие, которое никогда не попадает в публичный индекс."""

    sample_id: UUID
    sample_state: str | None = None

    def change(self) -> SampleFeatureChange:
        """Преобразовать результат приватного этапа в прикладные данные."""
        extracted_text, vectors = self.content()
        return SampleFeatureChange(
            event_id=self.event_id,
            sample_id=self.sample_id,
            cycle_id=self.cycle_id,
            version_key=self.feature_version.key(),
            embedding_version=self.embedding_version(),
            kind=self.kind,
            generation=self.generation,
            state=self.state,
            sample_state=SampleState(self.sample_state)
            if self.sample_state in {SampleState.READY, SampleState.FAILED}
            else None,
            text=extracted_text,
            vectors=vectors,
        )


class MediaStateBody(BaseModel):
    """Tombstone или актуальное членство одного медиа."""

    event_id: UUID
    media_id: UUID
    revision: int = Field(ge=1)
    active: bool
    group_id: UUID | None = None
    group_revision: int | None = Field(default=None, ge=1)
    composition_revision: int | None = Field(default=None, ge=1)
    kind: SearchMediaKind | None = Field(
        default=None, validation_alias=AliasChoices("kind", "media_kind")
    )
    published_at: datetime | None = None

    def change(self) -> MediaStateChange:
        """Преобразовать транспорт в независимые данные."""
        return MediaStateChange(
            event_id=self.event_id,
            media_id=self.media_id,
            revision=self.revision,
            active=self.active,
            group_id=self.group_id,
            group_revision=self.composition_revision or self.group_revision,
            kind=self.kind,
            published_at=self.published_at,
        )
