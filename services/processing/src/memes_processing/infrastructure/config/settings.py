"""Настройки процессов координатора из переменных окружения."""

import json
from datetime import timedelta
from enum import IntEnum, StrEnum
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator

from memes_platform.infrastructure.configuration import BaseServiceSettings
from memes_processing.application.dto.coordinator import (
    CallbackTarget,
    CoordinatorLimit,
    CoordinatorPolicy,
)
from memes_processing.application.dto.features import mapping, parse_version
from memes_processing.application.tasks import RetryPolicy, TaskLimit


class ProcessingDefault(IntEnum):
    """Нейтральные значения процессов."""

    OUTBOX_INTERVAL_SECONDS = 1
    SOURCE_URL_SECONDS = 60
    MODEL_DIMENSIONS = 768
    OUTBOX_MAX_DELAY = 300
    OUTBOX_MAX_EXPONENT = 8
    OUTBOX_FACTOR = 2


class ModelName(StrEnum):
    """Явные версии адаптеров и весов релиза."""

    PREVIEW = "ffmpeg"
    PREVIEW_VERSION = "builtin-v1"
    OCR = "PaddleOCR"
    OCR_WEIGHTS = "PP-OCRv4-ru-en@2.10.0"
    ASR = "Systran/faster-whisper-small"
    ASR_WEIGHTS = "536b0662742c02347bc0e980a01041f333bce120"
    EMBEDDING = "jinaai/jina-embeddings-v5-omni-nano"
    EMBEDDING_WEIGHTS = "009e0d09a98a82227526e8c5aa7b0aa282e36163"
    ADAPTER = "retrieval-v1"
    PREPROCESSING = "ffmpeg-sampling-v1"
    SPACE = "jina-v5-omni-nano"


class ProcessingSettings(BaseServiceSettings):
    """Конфигурация HTTP, scheduling и модельного контракта."""

    identity_url: str = Field(min_length=1)
    catalog_url: str = Field(min_length=1)
    search_url: str = Field(min_length=1)
    shadow_search_url: str = ""
    s3_public_endpoint_url: str | None = None
    source_url_seconds: int = Field(
        default=ProcessingDefault.SOURCE_URL_SECONDS, ge=1, le=ProcessingDefault.SOURCE_URL_SECONDS
    )
    s3_bucket: str = Field(min_length=1)
    s3_access_key: str = Field(min_length=1, repr=False)
    s3_secret_key: str = Field(min_length=1, repr=False)
    lease_seconds: int = Field(default=TaskLimit.LEASE_SECONDS, ge=1)
    task_seconds: int = Field(default=TaskLimit.TASK_SECONDS, ge=1)
    maximum_retries: int = Field(default=TaskLimit.RETRIES, ge=0)
    retry_base_seconds: int = Field(default=TaskLimit.BASE_DELAY_SECONDS, ge=1)
    retry_factor: int = Field(default=TaskLimit.BACKOFF_FACTOR, ge=1)
    retry_max_seconds: int = Field(default=TaskLimit.MAX_DELAY_SECONDS, ge=1)
    retry_jitter_fraction: float = Field(
        default=TaskLimit.JITTER_PERCENT / TaskLimit.PERCENT_SCALE, ge=0, le=1
    )
    long_poll_seconds: int = Field(default=CoordinatorLimit.POLL_SECONDS, ge=1)
    outbox_interval_seconds: float = Field(default=ProcessingDefault.OUTBOX_INTERVAL_SECONDS, gt=0)
    result_max_bytes: int = Field(default=CoordinatorLimit.RESULT_BYTES, ge=1)
    model_versions_json: str = ""
    embedding_dimensions: int = Field(
        default=ProcessingDefault.MODEL_DIMENSIONS, ge=1, le=ProcessingDefault.MODEL_DIMENSIONS
    )
    embedding_space_id: str = ModelName.SPACE
    embedding_weights: str = ModelName.EMBEDDING_WEIGHTS
    asr_weights: str = ModelName.ASR_WEIGHTS

    @field_validator("identity_url", "catalog_url", "search_url")
    @classmethod
    def validate_service_url(cls, value: str) -> str:
        """Подключаемые сервисы должны иметь абсолютные адреса HTTP."""
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("invalid_backing_service_url")
        return value.rstrip("/")

    @field_validator("shadow_search_url")
    @classmethod
    def validate_optional_shadow_url(cls, value: str) -> str:
        """Пустой адрес отключает shadow routing до приёма источника."""
        return cls.validate_service_url(value) if value else value

    @model_validator(mode="after")
    def validate_time_budget(self) -> "ProcessingSettings":
        """Проверить допустимость lease и backoff до открытия ресурсов."""
        if self.lease_seconds > self.task_seconds:
            raise ValueError("lease_exceeds_attempt_deadline")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("retry_maximum_less_than_base")
        self.policy()
        return self

    def policy(self) -> CoordinatorPolicy:
        """Построить прикладную политику без передачи settings внутрь слоя."""
        from_payload = (
            mapping(json.loads(self.model_versions_json)) if self.model_versions_json else None
        )
        versions = {}
        for kind in (
            "preview",
            "ocr",
            "transcript",
            "visual_embedding",
            "audio_embedding",
            "text_embedding",
        ):
            if from_payload is not None:
                version = parse_version(mapping(from_payload.get(kind)))
                versions[kind] = version
                if (
                    version.model == ModelName.EMBEDDING
                    and version.dimensions is not None
                    and version.dimensions > ProcessingDefault.MODEL_DIMENSIONS
                ):
                    raise ValueError("embedding_dimensions_exceed_model_capacity")
                continue
            model, weights = ModelName.EMBEDDING, self.embedding_weights
            space: str | None = self.embedding_space_id
            dimensions: int | None = self.embedding_dimensions
            if kind == "preview":
                model, weights, space, dimensions = (
                    ModelName.PREVIEW,
                    ModelName.PREVIEW_VERSION,
                    None,
                    None,
                )
            elif kind == "ocr":
                model, weights, space, dimensions = ModelName.OCR, ModelName.OCR_WEIGHTS, None, None
            elif kind == "transcript":
                model, weights, space, dimensions = ModelName.ASR, self.asr_weights, None, None
            versions[kind] = parse_version(
                {
                    "model": model,
                    "weights": weights,
                    "task_adapter": ModelName.ADAPTER,
                    "preprocessing": ModelName.PREPROCESSING,
                    "space": space,
                    "dimensions": dimensions,
                }
            )
        return CoordinatorPolicy(
            versions=versions,
            retries=RetryPolicy(
                maximum_retries=self.maximum_retries,
                base_delay=timedelta(seconds=self.retry_base_seconds),
                factor=self.retry_factor,
                maximum_delay=timedelta(seconds=self.retry_max_seconds),
                jitter_fraction=self.retry_jitter_fraction,
            ),
            lease_duration=timedelta(seconds=self.lease_seconds),
            attempt_duration=timedelta(seconds=self.task_seconds),
            result_bytes=self.result_max_bytes,
            callback_targets=frozenset(CallbackTarget)
            if self.shadow_search_url
            else frozenset({CallbackTarget.PRIMARY}),
        )
