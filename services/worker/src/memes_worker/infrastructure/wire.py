"""Валидация границ сериализации инфраструктурных адаптеров."""

from typing import cast

from memes_worker.application.dto.runtime import Payload, WorkerFailure, WorkerTransportError


def object_payload(value: object) -> Payload:
    """Валидация инфраструктурной границы без Any в прикладном слое."""
    if not isinstance(value, dict):
        raise WorkerTransportError(WorkerFailure.INVALID_RESPONSE)
    return cast(Payload, value)


def value_string(value: object) -> str:
    """Проверить обязательную строку wire-контракта."""
    if not isinstance(value, str):
        raise WorkerTransportError(WorkerFailure.INVALID_RESPONSE)
    return value


def value_integer(value: object) -> int:
    """Логическое значение не является размером файла."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkerTransportError(WorkerFailure.INVALID_RESPONSE)
    return value
