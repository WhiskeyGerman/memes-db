"""Сценарии доверенного административного процесса Identity."""

from datetime import timedelta
from enum import IntEnum
from uuid import uuid4

from memes_identity.application.dto.authentication import PasswordPolicy, UserView
from memes_identity.application.errors import IdentityError, IdentityFailure
from memes_identity.application.ports.clock import IClock
from memes_identity.application.ports.maintenance import IIdentityMaintenance
from memes_identity.application.ports.passwords import IPasswordVerifier
from memes_identity.application.ports.unit_of_work import IIdentityUnitOfWork
from memes_identity.application.use_cases.accounts import user_view
from memes_identity.domain.user import Login, User, UserId, UserRole


class Retention(IntEnum):
    """Сроки хранения технических завершённых записей."""

    AUDIT_DAYS = 30
    RATE_WINDOWS = 2


class OperatorUseCases:
    """Bootstrap оператора из доверенного one-off процесса релиза."""

    def __init__(
        self,
        *,
        uow: IIdentityUnitOfWork,
        passwords: IPasswordVerifier,
        clock: IClock,
        policy: PasswordPolicy,
    ) -> None:
        """Получить прикладные зависимости.

        Args:
            uow: Транзакция аккаунта, verifier и сессий.
            passwords: Вычисление verifier пароля.
            clock: Серверное время.
            policy: Допустимость исходного пароля.
        """
        self._uow = uow
        self._passwords = passwords
        self._clock = clock
        self._policy = policy

    async def bootstrap(self, *, login: str, password: str | None) -> UserView:
        """Создать оператора либо восстановить его доступ с отзывом сессий.

        Сценарий доступен только доверенному административному процессу.
        Пароль существующего аккаунта не меняется при повышении роли.

        Args:
            login: Логин создаваемого или существующего аккаунта.
            password: Пароль, обязательный только при создании аккаунта.

        Returns:
            UserView: Безопасное представление оператора.

        Raises:
            IdentityError: Нет допустимого пароля либо аккаунт изменился.
        """
        normalized = Login(value=login)
        now = self._clock.now()
        async with self._uow:
            user = await self._uow.users.find_by_login(normalized)
            if user is None:
                if password is None or not self._policy.accepts(password):
                    raise IdentityError(IdentityFailure.INVALID_PASSWORD_LENGTH)
                user = User(
                    id=UserId(uuid4()),
                    login=normalized,
                    created_at=now,
                    roles=frozenset({UserRole.USER, UserRole.OPERATOR}),
                )
                await self._uow.users.save(user, None)
                await self._uow.credentials.set(
                    user.id, await self._passwords.encode(password), now
                )
            else:
                current = await self._uow.users.get(user.id, lock=True)
                if current is None:
                    raise IdentityError(IdentityFailure.NOT_FOUND)
                user = current.change_access(
                    blocked=False, roles=frozenset({UserRole.USER, UserRole.OPERATOR})
                )
                if user is not current:
                    await self._uow.users.save(user, current.revision)
                await self._uow.sessions.revoke_all(current.id, now)
            await self._uow.commit()
        return user_view(user)


class IdentityMaintenanceUseCases:
    """Прикладная политика retention завершённых технических данных."""

    def __init__(
        self, *, repository: IIdentityMaintenance, clock: IClock, login_window_seconds: int
    ) -> None:
        """Получить зависимости очистки.

        Args:
            repository: Атомарное хранилище технических записей.
            clock: Серверное время.
            login_window_seconds: Длительность распределённого окна входа.
        """
        self._repository = repository
        self._clock = clock
        self._login_window_seconds = login_window_seconds

    async def cleanup(self) -> None:
        """Рассчитать сроки и сохранить незавершённые доставки и tombstone."""
        now = self._clock.now()
        await self._repository.purge(
            sessions_before=now,
            rates_before=now
            - timedelta(seconds=self._login_window_seconds * Retention.RATE_WINDOWS),
            deliveries_before=now - timedelta(days=Retention.AUDIT_DAYS),
        )
