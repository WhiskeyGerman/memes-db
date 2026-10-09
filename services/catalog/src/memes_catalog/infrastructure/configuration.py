"""Конфигурация Catalog, читаемая исключительно из окружения релиза."""

from datetime import timedelta
from enum import IntEnum, StrEnum
from typing import Self
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import Field, field_validator, model_validator

from memes_catalog.application.dto.catalog import CatalogDefault, SearchIndexTarget
from memes_catalog.domain.media import MediaLimit, MediaPolicy
from memes_catalog.infrastructure.media.inspector import InspectionLimit
from memes_platform.infrastructure.configuration import BaseServiceSettings


class VotingDefault(IntEnum):
    """Начальные продуктовые пороги."""

    MERGE = 1
    DETACH = 10


class CatalogSetting(StrEnum):
    """Версия начальной политики и название сервиса."""

    POLICY = "memes-db/variation-policy/v1"
    TITLE = "Memes catalog"


class CatalogSettings(BaseServiceSettings):
    """Пределы приёмки и адреса ресурсов конкретного развёртывания."""

    identity_url: str = Field(min_length=1)
    processing_url: str = Field(min_length=1)
    search_url: str = Field(min_length=1)
    shadow_search_url: str | None = None
    active_search_index: SearchIndexTarget = SearchIndexTarget.PRIMARY
    s3_access_key: str = Field(min_length=1, repr=False)
    s3_secret_key: str = Field(min_length=1, repr=False)
    s3_bucket: str = Field(min_length=1)
    journal_bucket: str = Field(min_length=1)
    s3_public_endpoint_url: str | None = None
    maximum_media_bytes: int = Field(
        default=MediaLimit.MAXIMUM_BYTES, ge=1, le=MediaLimit.MAXIMUM_BYTES
    )
    maximum_media_seconds: int = Field(
        default=MediaLimit.MAXIMUM_SECONDS, ge=1, le=MediaLimit.MAXIMUM_SECONDS
    )
    concurrent_uploads: int = Field(default=CatalogDefault.CONCURRENT_UPLOADS, ge=1)
    daily_publications: int = Field(default=CatalogDefault.DAILY_PUBLICATIONS, ge=1)
    storage_capacity_bytes: int = Field(default=CatalogDefault.STORAGE_BYTES, ge=1)
    free_storage_percent: int = Field(
        default=CatalogDefault.FREE_PERCENT, ge=1, lt=CatalogDefault.PERCENT_SCALE
    )
    processing_queue_limit: int = Field(default=CatalogDefault.QUEUE_LIMIT, ge=1)
    upload_seconds: int = Field(default=CatalogDefault.UPLOAD_SECONDS, ge=1)
    claim_seconds: int = Field(default=CatalogDefault.CLAIM_SECONDS, ge=1)
    merge_threshold: int = Field(default=VotingDefault.MERGE, ge=1)
    detach_threshold: int = Field(default=VotingDefault.DETACH, ge=1)
    voting_policy_revision: UUID = uuid5(NAMESPACE_URL, CatalogSetting.POLICY)
    vote_rate_per_minute: int = Field(default=CatalogDefault.VOTES_PER_MINUTE, ge=1)
    outbox_batch_size: int = Field(default=CatalogDefault.OUTBOX_BATCH, ge=1)
    maintenance_interval_seconds: float = Field(default=CatalogDefault.OUTBOX_RETRY_SECONDS, gt=0)
    ffprobe_path: str = "ffprobe"
    inspection_concurrency: int = Field(default=InspectionLimit.CONCURRENCY, ge=1)
    inspection_timeout_seconds: int = Field(default=InspectionLimit.SECONDS, ge=1)

    @field_validator("shadow_search_url")
    @classmethod
    def valid_shadow_url(cls, value: str | None) -> str | None:
        """Допустить только отдельно сконфигурированный внутренний REST origin."""
        if value is None:
            return None
        parts = urlsplit(value)
        if (
            parts.scheme not in ("http", "https")
            or not parts.netloc
            or parts.username
            or parts.password
        ):
            raise ValueError("MEMES_SHADOW_SEARCH_URL must be an HTTP resource without credentials")
        return value

    @model_validator(mode="after")
    def require_active_search(self) -> Self:
        """Не переключать чтение вариаций на отсутствующий ресурс."""
        if self.active_search_index is SearchIndexTarget.SHADOW and self.shadow_search_url is None:
            raise ValueError("shadow suggestions require MEMES_SHADOW_SEARCH_URL")
        return self

    def media_policy(self) -> MediaPolicy:
        """Собрать независимую доменную политику."""
        return MediaPolicy(
            maximum_bytes=self.maximum_media_bytes,
            maximum_duration=timedelta(seconds=self.maximum_media_seconds),
        )
