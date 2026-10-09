"""Прикладные сообщения координатора без зависимостей от HTTP и ORM."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from typing import TypeAlias
from uuid import UUID

from memes_kernel.values import FileRef
from memes_processing.application.tasks import ProcessingTask, RetryPolicy, TaskLimit
from memes_processing.domain.features import FeatureVersion

JSONValue: TypeAlias = bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"] | None
Payload: TypeAlias = dict[str, JSONValue]


class SourceType(StrEnum):
    """Назначение оригинала и адрес результата."""

    MEDIA = "media"
    SAMPLE = "sample"
    QUERY = "query"


class CallbackTarget(StrEnum):
    """Разрешённые логические получатели приватных образцов."""

    PRIMARY = "primary"
    SHADOW = "shadow"


class EventDestination(StrEnum):
    """Назначение durable события независимо от вида входного источника."""

    MEDIA = "media"
    SAMPLE = "sample"
    SHADOW_SAMPLE = "sample_shadow"


class MediaKind(StrEnum):
    """Модальности входного файла."""

    IMAGE = "image"
    ANIMATION = "animation"
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"


class CoordinatorCode(StrEnum):
    """Машинные причины отказа прикладного сценария."""

    NOT_FOUND = "not_found"
    UNAUTHORIZED = "unauthorized"
    UNAVAILABLE = "unavailable"
    FORBIDDEN = "forbidden"
    SOURCE_DELETED = "source_deleted"
    SOURCE_CONFLICT = "source_conflict"
    WRONG_VERSION = "wrong_version"
    INVALID_RESULT = "invalid_result"
    CAPACITY_REACHED = "capacity_reached"


class CoordinatorError(ValueError):
    """Ожидаемый прикладной отказ."""

    def __init__(self, code: CoordinatorCode) -> None:
        """Сохранить причину отказа.

        Args:
            code: Причина отказа.
        """
        self.code = code
        super().__init__(code.value)


class CoordinatorLimit(IntEnum):
    """Начальные параметры технических процессов."""

    POLL_SECONDS = 30
    POLL_INTERVAL_MS = 500
    OUTBOX_BATCH = 100
    RESULT_BYTES = 8_388_608
    PREVIEW_BYTES = 2_097_152
    SOURCE_BYTES = 104_857_600
    FRAMES = 600
    TEXT_VECTORS = 128
    MILLISECONDS = 1000
    GPU_TASKS = 1


@dataclass(frozen=True, slots=True, kw_only=True)
class Source:
    """Состояние источника, принадлежащее координатору."""

    id: UUID
    type: SourceType
    kind: MediaKind
    file: FileRef
    object_key: str
    created_at: datetime
    metadata: Payload = field(default_factory=dict)
    expires_at: datetime | None = None
    deleted_at: datetime | None = None
    query_text: str | None = None
    revision: int = 1
    embedding_version: FeatureVersion | None = None
    callback_target: CallbackTarget = CallbackTarget.PRIMARY


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerDevice:
    """Отзываемые учетные данные доверенного устройства."""

    id: UUID
    name: str
    token_hash: str
    created_at: datetime
    revoked_at: datetime | None = None
    last_seen_at: datetime | None = None
    capabilities: Payload = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkItem:
    """Задание с оригиналом и полным fencing-контекстом."""

    task: ProcessingTask
    source: Source
    result: Payload | None = None
    accepted_attempt_id: UUID | None = None
    accepted_worker_id: UUID | None = None
    text_fragments: list[JSONValue] | None = None
    generation: int = 1


@dataclass(frozen=True, slots=True, kw_only=True)
class OutgoingEvent:
    """Надежная доставка результата через REST."""

    id: UUID
    destination: EventDestination
    source_id: UUID
    payload: Payload
    created_at: datetime
    attempts: int = 0
    ready_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CoordinatorPolicy:
    """Параметры обработки из конфигурации релиза."""

    versions: dict[str, FeatureVersion]
    retries: RetryPolicy = field(default_factory=RetryPolicy)
    lease_duration: timedelta = timedelta(seconds=TaskLimit.LEASE_SECONDS)
    attempt_duration: timedelta = timedelta(seconds=TaskLimit.TASK_SECONDS)
    result_bytes: int = CoordinatorLimit.RESULT_BYTES
    callback_targets: frozenset[CallbackTarget] = frozenset({CallbackTarget.PRIMARY})


@dataclass(frozen=True, slots=True, kw_only=True)
class Assignment:
    """Выдача задачи с временными ссылками только на ее объекты."""

    work: WorkItem
    download_url: str
    upload_url: str
    artifact_key: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RegisteredWorker:
    """Однократная выдача секрета оператору."""

    worker: WorkerDevice
    token: str = field(repr=False)
