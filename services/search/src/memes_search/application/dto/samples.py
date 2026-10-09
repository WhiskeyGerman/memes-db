"""Временные приватные образцы и результаты запроса inference."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from memes_kernel.values import FileRef, require_identifier, require_ordered
from memes_search.application.dto.indexes import IndexTarget
from memes_search.application.dto.projections import EmbeddingVector, EmbeddingVersion
from memes_search.application.dto.queries import SearchMediaKind


class SampleState(StrEnum):
    """Жизненный цикл приватного файла вне публичного каталога."""

    UPLOADING = "uploading"
    QUEUED = "queued"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SampleResultSource(StrEnum):
    """Источник готовых признаков временного образца."""

    CACHED = "cached"


class InferenceState(StrEnum):
    """Состояние вычисления вектора текстового запроса."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryInference:
    """Результат вычисления без технического lease воркера."""

    task_id: UUID
    state: InferenceState
    embedding: EmbeddingVector | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class PrivateSample:
    """Состояние временного образца, защищённого владельцем и сроком."""

    id: UUID
    owner_id: UUID
    object_key: str
    created_at: datetime
    expires_at: datetime
    state: SampleState = SampleState.UPLOADING
    file: FileRef | None = None
    kind: SearchMediaKind | None = None
    exact_media_id: UUID | None = None
    callback_target: IndexTarget = IndexTarget.PRIMARY
    failure: str | None = None
    embedding_version: EmbeddingVersion | None = None

    def __post_init__(self) -> None:
        """Проверить ключи владельца и ограниченный срок жизни."""
        require_identifier(self.id)
        require_identifier(self.owner_id)
        require_ordered(self.expires_at, self.created_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class SampleFeatures:
    """Поисковые признаки временного приватного образца."""

    text: str = field(default="", repr=False)
    vectors: tuple[EmbeddingVector, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class SampleFeatureChange:
    """Идемпотентное событие отдельного этапа приватного образца."""

    event_id: UUID
    sample_id: UUID
    kind: str
    generation: int
    state: str
    text: str = field(default="", repr=False)
    vectors: tuple[EmbeddingVector, ...] = field(default=(), repr=False)

    sample_state: SampleState | None = None
    cycle_id: UUID | None = None
    version_key: str = ""
    embedding_version: EmbeddingVersion | None = None
