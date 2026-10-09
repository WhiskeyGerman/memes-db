"""Прикладные контракты клиента."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias

from memes_worker.application.execution import Assignment, ExecutionKey

JSONValue: TypeAlias = bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"] | None
Payload: TypeAlias = dict[str, JSONValue]


class WorkerFailure(StrEnum):
    """Санитизированные причины отказа."""

    TEMPORARY = "temporary"
    INVALID_INPUT = "invalid_input"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    OWNERSHIP_LOST = "ownership_lost"
    INVALID_RESPONSE = "invalid_response"


class WorkerExecutionError(ValueError):
    """Ожидаемая ошибка исполнителя."""

    def __init__(self, reason: WorkerFailure) -> None:
        """Сохранить безопасную причину."""
        self.reason = reason
        super().__init__(reason.value)


class WorkerTransportError(ConnectionError):
    """Сбой транспорта либо отзыв права."""

    def __init__(self, reason: WorkerFailure) -> None:
        """Сохранить безопасную причину."""
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkAssignment:
    """Назначение с ограниченными временными ссылками."""

    assignment: Assignment
    source_type: str
    media_kind: str
    feature_version: Payload
    download_url: str = field(repr=False)
    upload_url: str = field(repr=False)
    preview_upload_url: str = field(repr=False)
    preview_key: str
    query_text: str | None = None
    metadata: Payload = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkResult:
    """Кэш содержит результат без исходных файлов."""

    key: ExecutionKey
    payload: Payload
    preview: bytes | None = field(default=None, repr=False)
