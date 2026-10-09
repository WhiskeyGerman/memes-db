"""Контракты технического исполнителя без выдуманного бизнес-домена."""

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from uuid import UUID

from memes_kernel.values import (
    FileRef,
    Number,
    Sha256,
    require_identifier,
    require_ordered,
)


class Operation(StrEnum):
    """Операции из версионированного протокола координатора."""

    PREVIEW = "preview"
    OCR = "ocr"
    TRANSCRIPT = "transcript"
    VISUAL_EMBEDDING = "visual_embedding"
    AUDIO_EMBEDDING = "audio_embedding"
    TEXT_EMBEDDING = "text_embedding"


class WorkerLimit(IntEnum):
    """Начальные параметры доверенного исполнителя."""

    RENEWAL_SECONDS = 20
    CACHE_SECONDS = 300
    CACHE_BYTES = 2_147_483_648


class WorkerErrorCode(StrEnum):
    """Причины отклонения входного контракта исполнителя."""

    INVALID_CAPABILITIES = "invalid_worker_capabilities"
    INVALID_EXECUTION_KEY = "invalid_execution_key"
    INVALID_ASSIGNMENT = "invalid_worker_assignment"


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerCapabilities:
    """Операции и ресурсы, объявляемые при регистрации устройства."""

    operations: frozenset[Operation]
    maximum_parallel_tasks: int
    gpu_memory_bytes: int | None = None

    def __post_init__(self) -> None:
        """Проверить объявленные ресурсы устройства."""
        if (
            not self.operations
            or self.maximum_parallel_tasks < Number.ONE
            or (self.gpu_memory_bytes is not None and self.gpu_memory_bytes < Number.ONE)
        ):
            raise ValueError(WorkerErrorCode.INVALID_CAPABILITIES)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionKey:
    """Ключ повторного использования результата только для того же входа.

    SHA-256 не является единственным ключом: отдельный ID источника и цикл
    исключают перенос приватного результата между разными публикациями.
    """

    source_id: UUID
    source_hash: Sha256
    cycle_id: UUID
    operation: Operation
    fragment_id: UUID
    model_revision: str
    preprocessing_revision: str

    def __post_init__(self) -> None:
        """Проверить полноту идентичности входа."""
        for identifier in (self.source_id, self.cycle_id, self.fragment_id):
            require_identifier(identifier)
        if not self.model_revision.strip() or not self.preprocessing_revision.strip():
            raise ValueError(WorkerErrorCode.INVALID_EXECUTION_KEY)


@dataclass(frozen=True, slots=True, kw_only=True)
class Assignment:
    """Входной DTO протокола исходящего соединения с координатором."""

    task_id: UUID
    attempt_id: UUID
    source: FileRef
    key: ExecutionKey
    lease_expires_at: datetime
    deadline: datetime

    def __post_init__(self) -> None:
        """Проверить соответствие файла ключу и границы назначения."""
        require_identifier(self.task_id)
        require_identifier(self.attempt_id)
        require_ordered(self.deadline, self.lease_expires_at)
        if self.source.sha256 != self.key.source_hash:
            raise ValueError(WorkerErrorCode.INVALID_ASSIGNMENT)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionResult:
    """Сериализованный результат; схема определяется версией протокола."""

    key: ExecutionKey
    artifact: FileRef
