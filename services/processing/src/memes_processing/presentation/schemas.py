"""Валидация REST-контрактов до входа в прикладной слой."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from memes_processing.application.dto.coordinator import CallbackTarget, CoordinatorLimit, MediaKind
from memes_processing.application.tasks import TaskFailure
from memes_processing.domain.features import FeatureKind


class StrictSchema(BaseModel):
    """Запретить неизвестные поля межсервисного протокола."""

    model_config = ConfigDict(extra="forbid")


class VersionSchema(StrictSchema):
    """Полный паспорт совместимости модели."""

    model: str = Field(min_length=1)
    weights: str = Field(min_length=1)
    task_adapter: str = Field(min_length=1)
    preprocessing: str = Field(min_length=1)
    space: str | None = None
    dimensions: int | None = Field(default=None, ge=1)


class FileSchema(StrictSchema):
    """Идентичность входа без файловых байтов."""

    id: UUID
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    size_bytes: int = Field(ge=1, le=CoordinatorLimit.SOURCE_BYTES)
    object_key: str | None = None


class PublishedSchema(StrictSchema):
    """Идемпотентное событие публикации каталога."""

    event_id: UUID
    media_id: UUID
    file_id: UUID
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    size_bytes: int = Field(ge=1, le=CoordinatorLimit.SOURCE_BYTES)
    media_kind: MediaKind
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    occurred_at: datetime
    source_revision: int = Field(default=1, ge=1)
    object_key: str | None = None


class DeletedSchema(StrictSchema):
    """Удаление блокирует результаты всех прежних назначений."""

    event_id: UUID
    media_id: UUID
    occurred_at: datetime


class SampleSchema(StrictSchema):
    """Приватный поисковый источник с жестким сроком жизни."""

    sample_id: UUID
    owner_id: UUID
    file: FileSchema
    media_kind: MediaKind
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    expires_at: datetime
    callback_target: CallbackTarget = CallbackTarget.PRIMARY
    feature_version: VersionSchema | None = None


class QuerySchema(StrictSchema):
    """Поисковый текст не является публичной публикацией."""

    request_id: UUID
    owner_id: UUID | None = None
    text: str = Field(min_length=1, max_length=4096)
    space_id: str = Field(min_length=1)
    feature_version: VersionSchema | None = None


class WorkerCapabilitiesSchema(StrictSchema):
    """Только совместимые этапы могут быть назначены устройству."""

    maximum_parallel_tasks: int = Field(default=1, ge=1, le=32)
    versions: dict[FeatureKind, VersionSchema] = Field(min_length=1, max_length=6)
    cpu_cores: int = Field(default=1, ge=1)
    gpu_memory_bytes: int | None = Field(default=None, ge=1)
    cached_source_ids: list[UUID] = Field(default_factory=list, max_length=1024)


class RegisterWorkerSchema(StrictSchema):
    """Имя операторской регистрации."""

    name: str = Field(min_length=1, max_length=120)


class AttemptSchema(StrictSchema):
    """Fencing ID выданной попытки."""

    attempt_id: UUID


class CompletionSchema(AttemptSchema):
    """Контрольная сумма загруженного JSON-результата."""

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FailureSchema(AttemptSchema):
    """Санитизированная причина без трассировки и секретов."""

    reason: TaskFailure


class RetrySchema(StrictSchema):
    """Повтор независимого этапа создает новое поколение."""

    kind: FeatureKind
    feature_version: VersionSchema | None = None


HeaderToken = Annotated[str | None, Field(default=None)]
