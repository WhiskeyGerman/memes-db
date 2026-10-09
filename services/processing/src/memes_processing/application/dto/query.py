"""Прикладной запрос приватного query inference."""

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from memes_processing.domain.features import FeatureVersion


class QueryState(StrEnum):
    """Общий контракт ожидания поискового вектора."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryRequest:
    """Приватный текст с идентичностью отменяемого запроса."""

    request_id: UUID
    text: str = field(repr=False)
    space_id: str
    owner_id: UUID | None = None
    feature_version: FeatureVersion | None = None
