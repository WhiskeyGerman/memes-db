"""Минимальный подтверждённый субъект публичных поисковых запросов."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class Principal:
    """Идентичность без паролей, токенов и DTO соседнего сервиса."""

    user_id: UUID
