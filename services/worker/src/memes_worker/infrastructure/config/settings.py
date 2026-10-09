"""Конфигурация standalone клиента исключительно из окружения."""

import json
from enum import IntEnum, StrEnum
from os import cpu_count
from pathlib import Path
from typing import cast
from urllib.parse import urlparse
from uuid import UUID

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from memes_worker.application.dto.runtime import Payload, WorkerExecutionError, WorkerFailure
from memes_worker.application.execution import WorkerLimit
from memes_worker.infrastructure.ml.manifest import verify_manifest
from memes_worker.infrastructure.ml.model_config import validate_model_capacity
from memes_worker.infrastructure.ml.ocr_config import validate_ocr_directory
from memes_worker.infrastructure.wire import object_payload


class WorkerDefault(IntEnum):
    """Нейтральные ресурсы одного устройства."""

    PARALLEL_TASKS = 1
    HTTP_SECONDS = 45
    PROCESS_SECONDS = 600
    CHUNK_BYTES = 65536
    FRAME_LIMIT = 600
    FPS = 2
    VIDEO_FRAGMENT_SECONDS = 10
    AUDIO_FRAGMENT_SECONDS = 30
    OVERLAP_SECONDS = 2
    MAX_SOURCE_BYTES = 104857600
    MAX_RESULT_BYTES = 8388608
    EMBEDDING_DIMENSIONS = 768


class WorkerText(StrEnum):
    """Фиксированные имена адаптеров и версий."""

    PREFIX = "MEMES_WORKER_"
    CACHE = "/var/cache/memes-worker"
    OPERATIONS = "preview"
    DEVICE = "cpu"
    FFMPEG = "ffmpeg"
    FFPROBE = "ffprobe"
    MODEL = "jinaai/jina-embeddings-v5-omni-nano"
    WEIGHTS = "009e0d09a98a82227526e8c5aa7b0aa282e36163"
    ASR_WEIGHTS = "536b0662742c02347bc0e980a01041f333bce120"
    SPACE = "jina-v5-omni-nano"
    ADAPTER = "retrieval-v1"
    PREPROCESSING = "ffmpeg-sampling-v1"


class WorkerSettings(BaseSettings):
    """Устройство не получает credentials PostgreSQL или общий ключ S3."""

    model_config = SettingsConfigDict(
        env_prefix=WorkerText.PREFIX, extra="ignore", hide_input_in_errors=True
    )
    coordinator_url: str
    worker_id: UUID
    token: str = Field(min_length=32, repr=False)
    release_id: str = Field(min_length=1)
    allow_insecure_http: bool = False
    gpu_memory_bytes: int | None = Field(default=None, ge=1)
    maximum_parallel_tasks: int = Field(default=WorkerDefault.PARALLEL_TASKS, ge=1, le=32)
    renewal_seconds: float = Field(default=WorkerLimit.RENEWAL_SECONDS, gt=0)
    http_timeout_seconds: float = Field(default=WorkerDefault.HTTP_SECONDS, gt=0)
    cache_seconds: int = Field(default=WorkerLimit.CACHE_SECONDS, ge=1)
    cache_bytes: int = Field(default=WorkerLimit.CACHE_BYTES, ge=1)
    cache_directory: Path = Path(WorkerText.CACHE)
    process_seconds: int = Field(default=WorkerDefault.PROCESS_SECONDS, ge=1)
    operations: str = WorkerText.OPERATIONS
    model_versions_json: str = ""
    ffmpeg: str = WorkerText.FFMPEG
    ffprobe: str = WorkerText.FFPROBE
    ml_python: str = ""
    device: str = WorkerText.DEVICE
    jina_model_directory: Path | None = None
    jina_revision: str = WorkerText.WEIGHTS
    asr_model_directory: Path | None = None
    asr_revision: str = WorkerText.ASR_WEIGHTS
    ocr_use_gpu: bool = False
    ocr_detection_directory: Path | None = None
    ocr_ru_directory: Path | None = None
    ocr_en_directory: Path | None = None
    embedding_space_id: str = WorkerText.SPACE
    embedding_dimensions: int = Field(
        default=WorkerDefault.EMBEDDING_DIMENSIONS, ge=1, le=WorkerDefault.EMBEDDING_DIMENSIONS
    )
    health_host: str = "127.0.0.1"
    health_port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = "INFO"

    @field_validator("coordinator_url")
    @classmethod
    def absolute_url(cls, value: str) -> str:
        """Потребовать абсолютный адрес подключаемого координатора."""
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
        ):
            raise ValueError("invalid_coordinator_url")
        return value.rstrip("/")

    def capabilities(self) -> Payload:
        """Объявлять лишь реально установленные модели."""
        if urlparse(self.coordinator_url).scheme != "https" and not self.allow_insecure_http:
            raise WorkerExecutionError(WorkerFailure.INVALID_RESPONSE)
        operations = set(self.operations.split(","))
        custom = (
            cast(Payload, json.loads(self.model_versions_json)) if self.model_versions_json else {}
        )
        versions: Payload = {}
        for operation in operations:
            model: str = WorkerText.MODEL
            weights: str = self.jina_revision
            space: str | None = self.embedding_space_id
            dimensions: int | None = self.embedding_dimensions
            if operation == "preview":
                model, weights, space, dimensions = "ffmpeg", "builtin-v1", None, None
            elif operation == "ocr":
                if not all(
                    path is not None and path.is_dir()
                    for path in (
                        self.ocr_detection_directory,
                        self.ocr_ru_directory,
                        self.ocr_en_directory,
                    )
                ):
                    raise WorkerExecutionError(WorkerFailure.INVALID_INPUT)
                model, weights, space, dimensions = "PaddleOCR", "PP-OCRv4-ru-en@2.10.0", None, None
            elif operation == "transcript":
                if self.asr_model_directory is None or not self.asr_model_directory.is_dir():
                    raise WorkerExecutionError(WorkerFailure.INVALID_INPUT)
                model, weights, space, dimensions = (
                    "Systran/faster-whisper-small",
                    self.asr_revision,
                    None,
                    None,
                )
            elif operation in {"visual_embedding", "audio_embedding", "text_embedding"}:
                if self.jina_model_directory is None or not self.jina_model_directory.is_dir():
                    raise WorkerExecutionError(WorkerFailure.INVALID_INPUT)
            else:
                raise WorkerExecutionError(WorkerFailure.INVALID_INPUT)
            if operation == "ocr":
                for directory in (
                    self.ocr_detection_directory,
                    self.ocr_ru_directory,
                    self.ocr_en_directory,
                ):
                    if directory is not None:
                        verify_manifest(directory, model=str(model), revision=str(weights))
                        validate_ocr_directory(directory)
            elif operation == "transcript" and self.asr_model_directory is not None:
                verify_manifest(self.asr_model_directory, model=str(model), revision=str(weights))
            elif (
                operation in {"visual_embedding", "audio_embedding", "text_embedding"}
                and self.jina_model_directory is not None
            ):
                verify_manifest(self.jina_model_directory, model=str(model), revision=str(weights))
                validate_model_capacity(
                    self.jina_model_directory, dimensions=self.embedding_dimensions
                )
            selected = custom.get(
                operation,
                {
                    "model": model,
                    "weights": weights,
                    "task_adapter": WorkerText.ADAPTER,
                    "preprocessing": WorkerText.PREPROCESSING,
                    "space": space,
                    "dimensions": dimensions,
                },
            )
            selected_version = object_payload(selected)
            if (
                selected_version.get("model") != model
                or selected_version.get("weights") != weights
                or selected_version.get("dimensions") != dimensions
                or selected_version.get("space") != space
                or selected_version.get("task_adapter") != WorkerText.ADAPTER
                or selected_version.get("preprocessing") != WorkerText.PREPROCESSING
                or set(selected_version)
                != {"model", "weights", "task_adapter", "preprocessing", "space", "dimensions"}
            ):
                raise WorkerExecutionError(WorkerFailure.INVALID_INPUT)
            versions[operation] = selected_version
        return {
            "maximum_parallel_tasks": self.maximum_parallel_tasks,
            "versions": versions,
            "cpu_cores": cpu_count() or 1,
            "gpu_memory_bytes": self.gpu_memory_bytes,
        }
