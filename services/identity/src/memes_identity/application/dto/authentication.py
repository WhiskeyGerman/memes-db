"""Входные и выходные модели сценариев аутентификации."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from uuid import UUID

from memes_identity.application.errors import IdentityError, IdentityFailure
from memes_identity.domain.user import UserId, UserRole, UserState
from memes_kernel.values import require_aware, require_identifier


class AccountEventKind(StrEnum):
    """Тип собственного внешнего намерения удаления."""

    ACCOUNT = "account"


class PasswordLimit(IntEnum):
    """Параметры первой версии парольной политики."""

    MINIMUM_LENGTH = 12
    MAXIMUM_LENGTH = 128


@dataclass(frozen=True, slots=True, kw_only=True)
class PasswordPolicy:
    """Проверка допустимости пароля без преобразования содержимого."""

    minimum_length: int = PasswordLimit.MINIMUM_LENGTH
    maximum_length: int = PasswordLimit.MAXIMUM_LENGTH

    def __post_init__(self) -> None:
        """Проверить корректность настройки политики."""
        if not PasswordLimit.MINIMUM_LENGTH <= self.minimum_length <= self.maximum_length:
            raise IdentityError(IdentityFailure.INVALID_PASSWORD_LENGTH)

    def accepts(self, password: str) -> bool:
        """Проверить длину исходного пароля.

        Args:
            password: Пароль без нормализации.

        Returns:
            bool: Соответствует ли пароль политике.
        """
        return self.minimum_length <= len(password) <= self.maximum_length


@dataclass(frozen=True, slots=True, kw_only=True)
class IssuedTokens:
    """Результат выпуска непрозрачных токенов."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    access_expires_at: datetime
    refresh_expires_at: datetime
    session_id: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticatedActor:
    """Проверенная учётная запись и текущая сессия."""

    user_id: UserId
    login: str
    roles: frozenset[UserRole]
    session_id: UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class UserView:
    """Проекция аккаунта без персональных секретов."""

    user_id: UserId
    login: str | None
    roles: frozenset[UserRole]
    state: UserState
    delete_media: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionRotation:
    """Результат ротации, позволяющий зафиксировать обнаруженный replay."""

    tokens: IssuedTokens | None = None
    failure: IdentityFailure | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountDeleted:
    """Согласованное уведомление Catalog об удалении аккаунта."""

    event_id: UUID
    user_id: UserId
    delete_media: bool
    occurred_at: datetime

    def __post_init__(self) -> None:
        """Проверить безопасный контракт внешнего намерения."""
        require_identifier(self.event_id)
        require_identifier(self.user_id)
        require_aware(self.occurred_at)
