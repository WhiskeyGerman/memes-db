"""Записи механизма opaque-токенов; не доменные сущности."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class TokenKind(StrEnum):
    """Тип записи непрозрачного токена."""

    ACCESS = "access"
    REFRESH = "refresh"


@dataclass(frozen=True, slots=True, kw_only=True)
class CredentialRecord:
    """Сохраняемое представление парольных данных."""

    user_id: UUID
    verifier: str = field(repr=False)
    changed_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionRecord:
    """Запись семейства обновляемых токенов."""

    id: UUID
    user_id: UUID
    expires_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenRecord:
    """Запись токена с доказательством его использования."""

    id: UUID
    session_id: UUID
    kind: TokenKind
    digest: str = field(repr=False)
    issued_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    revoked_at: datetime | None
    replacement_id: UUID | None
