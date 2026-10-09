"""Пользователь и правила жизненного цикла учётной записи."""

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import IntEnum, StrEnum
from re import fullmatch
from typing import NewType
from uuid import UUID

from memes_kernel.entity import Entity
from memes_kernel.errors import DomainError
from memes_kernel.values import require_aware, require_ordered

UserId = NewType("UserId", UUID)


class UserState(StrEnum):
    """Состояние учётной записи."""

    ACTIVE = "active"
    BLOCKED = "blocked"
    DELETING = "deleting"
    DELETED = "deleted"


class UserRole(StrEnum):
    """Бизнес-роли участника."""

    USER = "user"
    OPERATOR = "operator"


class LoginLimit(IntEnum):
    """Границы длины логина."""

    MINIMUM = 3
    MAXIMUM = 32


class LoginPattern(StrEnum):
    """Разрешённый алфавит логина."""

    VALUE = r"[a-z0-9_-]+"


class AccountViolation(StrEnum):
    """Нарушения правил учётной записи."""

    INVALID_LOGIN = "invalid_login"
    INVALID_STATE = "invalid_account_state"
    INACTIVE = "inactive_account"
    DELETION_NOT_STARTED = "account_deletion_not_started"


@dataclass(frozen=True, slots=True, kw_only=True)
class Login:
    """Нормализованный регистронезависимый логин."""

    value: str

    def __post_init__(self) -> None:
        """Проверить и нормализовать логин без удаления символов."""
        normalized = self.value.lower()
        if (
            not self.value.isascii()
            or not LoginLimit.MINIMUM <= len(normalized) <= LoginLimit.MAXIMUM
            or fullmatch(LoginPattern.VALUE, normalized) is None
        ):
            raise DomainError(AccountViolation.INVALID_LOGIN)
        object.__setattr__(self, "value", normalized)


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class User(Entity[UserId]):
    """Пользователь без паролей, токенов и протоколов аутентификации."""

    login: Login | None
    created_at: datetime
    state: UserState = UserState.ACTIVE
    roles: frozenset[UserRole] = field(default_factory=lambda: frozenset({UserRole.USER}))
    deletion_requested_at: datetime | None = None
    deleted_at: datetime | None = None
    delete_media: bool = False

    def __post_init__(self) -> None:
        """Проверить согласованность жизненного цикла пользователя."""
        Entity.__post_init__(self)
        require_aware(self.created_at)
        if self.state is not UserState.DELETED and UserRole.USER not in self.roles:
            raise DomainError(AccountViolation.INVALID_STATE)
        if self.delete_media and self.state not in {UserState.DELETING, UserState.DELETED}:
            raise DomainError(AccountViolation.INVALID_STATE)
        if self.state is UserState.DELETED:
            if self.login is not None or self.deleted_at is None or self.roles:
                raise DomainError(AccountViolation.INVALID_STATE)
        elif self.login is None or self.deleted_at is not None:
            raise DomainError(AccountViolation.INVALID_STATE)
        if self.deletion_requested_at is not None:
            require_ordered(self.deletion_requested_at, self.created_at)
        if self.state in {UserState.DELETING, UserState.DELETED}:
            if self.deletion_requested_at is None:
                raise DomainError(AccountViolation.INVALID_STATE)
        elif self.deletion_requested_at is not None:
            raise DomainError(AccountViolation.INVALID_STATE)
        if self.deleted_at is not None:
            require_ordered(self.deleted_at, self.deletion_requested_at or self.created_at)

    def require_active(self) -> None:
        """Потребовать разрешённую для участия учётную запись.

        Raises:
            DomainError: Учётная запись неактивна.
        """
        if self.state is not UserState.ACTIVE:
            raise DomainError(AccountViolation.INACTIVE)

    def change_login(self, login: Login) -> "User":
        """Изменить логин активного пользователя после проверки уникальности.

        Args:
            login: Новый нормализованный логин.

        Returns:
            User: Состояние с новым логином либо прежнее при повторе.
        """
        self.require_active()
        if self.login == login:
            return self
        return replace(self, login=login, revision=self.revision.next())

    def change_access(self, *, blocked: bool, roles: frozenset[UserRole]) -> "User":
        """Изменить доступ и роли действующего либо заблокированного аккаунта.

        Args:
            blocked: Требуется ли заблокировать аккаунт.
            roles: Новый набор бизнес-ролей.

        Returns:
            User: Состояние после решения оператора.

        Raises:
            DomainError: Аккаунт уже удаляется либо отсутствует основная роль.
        """
        if self.state not in {UserState.ACTIVE, UserState.BLOCKED} or UserRole.USER not in roles:
            raise DomainError(AccountViolation.INVALID_STATE)
        state = UserState.BLOCKED if blocked else UserState.ACTIVE
        if self.state == state and self.roles == roles:
            return self
        return replace(self, state=state, roles=roles, revision=self.revision.next())

    def begin_deletion(self, *, now: datetime, delete_media: bool = False) -> "User":
        """Заблокировать участие после принятия решения об удалении.

        Args:
            now: Серверное время принятия решения.
            delete_media: Требуется ли удалить загруженные медиа.

        Returns:
            User: Новое состояние либо прежнее при повторе.
        """
        if self.state in {UserState.DELETING, UserState.DELETED}:
            return self
        require_ordered(now, self.created_at)
        return replace(
            self,
            state=UserState.DELETING,
            deletion_requested_at=now,
            delete_media=delete_media,
            revision=self.revision.next(),
        )

    def confirm_deletion_choice(self, *, delete_media: bool) -> "User":
        """Объединить повторные решения удаления монотонным логическим OR.

        Args:
            delete_media: Дополнительное авторизованное намерение удалить медиа.

        Returns:
            User: Состояние с сохранением самого строгого выбора пользователя.

        Raises:
            DomainError: Удаление аккаунта не началось.
        """
        if self.state not in {UserState.DELETING, UserState.DELETED}:
            raise DomainError(AccountViolation.DELETION_NOT_STARTED)
        if not delete_media or self.delete_media:
            return self
        return replace(self, delete_media=True, revision=self.revision.next())

    def finish_deletion(self, *, now: datetime) -> "User":
        """Обезличить аккаунт после запуска согласованного удаления.

        Args:
            now: Серверное время обезличивания.

        Returns:
            User: Обезличенное состояние пользователя.

        Raises:
            DomainError: Удаление не было начато.
        """
        if self.state is UserState.DELETED:
            return self
        if self.state is not UserState.DELETING:
            raise DomainError(AccountViolation.DELETION_NOT_STARTED)
        require_ordered(now, self.deletion_requested_at or self.created_at)
        return replace(
            self,
            login=None,
            roles=frozenset(),
            state=UserState.DELETED,
            deleted_at=now,
            revision=self.revision.next(),
        )
