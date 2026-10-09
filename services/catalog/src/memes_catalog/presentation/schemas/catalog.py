"""Публичные и внутренние HTTP-схемы каталога."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from memes_catalog.application.dto.catalog import CatalogDefault
from memes_catalog.domain.media import MediaLimit


class StrictSchema(BaseModel):
    """Запрет неизвестных полей входного контракта."""

    model_config = ConfigDict(extra="forbid")


class UploadRequest(StrictSchema):
    """Верхний предел одной передачи; расширению не доверяем."""

    size_bytes: int = Field(ge=1, le=MediaLimit.MAXIMUM_BYTES)


class MergeRequest(StrictSchema):
    """Голос за два конкретных снимка состава."""

    first_id: UUID
    second_id: UUID
    first_revision: int = Field(ge=1)
    second_revision: int = Field(ge=1)


class DismissRequest(StrictSchema):
    """Личное отклонение кандидата."""

    group_id: UUID
    group_revision: int = Field(ge=1)


class ResolveRequest(StrictSchema):
    """Ограниченная пачка актуальных публикаций."""

    media_ids: list[UUID] = Field(max_length=CatalogDefault.BATCH_SIZE)


class DeletedAccountRequest(StrictSchema):
    """Идемпотентное обезличивание автора из Identity."""

    event_id: UUID
    user_id: UUID
    delete_media: bool
    occurred_at: datetime


class InspectRequest(StrictSchema):
    """Приватный объект Search, который не публикуется."""

    object_key: str
    file_id: UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1, le=MediaLimit.MAXIMUM_BYTES)


class FeatureRequest(StrictSchema):
    """Полный паспорт результата независимого этапа."""

    event_id: UUID
    media_id: UUID | None = None
    cycle_id: UUID
    generation: int = Field(ge=1)
    source_revision: int = Field(ge=1)
    kind: str
    state: str
    feature_version: dict[str, JsonValue]
    result: dict[str, JsonValue] | None
