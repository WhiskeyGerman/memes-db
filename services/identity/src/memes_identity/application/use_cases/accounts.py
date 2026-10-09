"""Прикладные сценарии изменения и обезличивания аккаунта."""

from datetime import datetime
from uuid import UUID, uuid4

from memes_identity.application.dto.authentication import (
    AccountDeleted,
    AccountEventKind,
    AuthenticatedActor,
    PasswordPolicy,
    UserView,
)
from memes_identity.application.errors import IdentityError, IdentityFailure
from memes_identity.application.ports.clock import IClock
from memes_identity.application.ports.passwords import IPasswordVerifier
from memes_identity.application.ports.unit_of_work import IIdentityUnitOfWork
from memes_identity.domain.user import Login, User, UserId, UserRole, UserState
from memes_platform.application.ports.deletion_journal import IDeletionJournal


def user_view(user: User) -> UserView:
    """Построить безопасную проекцию аккаунта.

    Args:
        user: Бизнес-состояние аккаунта.

    Returns:
        UserView: Проекция без секретов и удалённого логина.
    """
    return UserView(
        user_id=user.id,
        login=user.login.value if user.login is not None else None,
        roles=user.roles,
        state=user.state,
        delete_media=user.delete_media,
    )


class AccountUseCases:
    """Транзакционные изменения пользовательского аккаунта."""

    def __init__(
        self,
        *,
        uow: IIdentityUnitOfWork,
        passwords: IPasswordVerifier,
        clock: IClock,
        policy: PasswordPolicy,
        journal: IDeletionJournal,
    ) -> None:
        """Получить прикладные зависимости.

        Args:
            uow: Транзакционная единица работы.
            passwords: Проверка паролей.
            clock: Серверное время.
            policy: Политика пароля.
            journal: Независимый журнал авторизованного удаления.
        """
        self._uow = uow
        self._passwords = passwords
        self._clock = clock
        self._policy = policy
        self._journal = journal

    async def me(self, actor: AuthenticatedActor) -> UserView:
        """Получить актуальное представление своего аккаунта.

        Args:
            actor: Проверенный владелец.

        Returns:
            UserView: Безопасные данные аккаунта.
        """
        async with self._uow:
            user = await self._active_user(actor.user_id)
            return user_view(user)

    async def batch(self, user_ids: tuple[UserId, ...]) -> tuple[UserView, ...]:
        """Получить безопасные представления авторов для соседнего сервиса.

        Args:
            user_ids: Идентификаторы аккаунтов.

        Returns:
            tuple[UserView, ...]: Найденные аккаунты, включая обезличенные tombstone.
        """
        async with self._uow:
            users = await self._uow.users.batch(tuple(dict.fromkeys(user_ids)))
        return tuple(user_view(user) for user in users)

    async def change_login(
        self, actor: AuthenticatedActor, *, login: str, password: str
    ) -> UserView:
        """Сменить логин после проверки текущего пароля.

        Args:
            actor: Проверенный владелец.
            login: Новый логин.
            password: Текущий пароль.

        Returns:
            UserView: Обновлённый аккаунт.
        """
        normalized = Login(value=login)
        async with self._uow:
            user = await self._active_user(actor.user_id, lock=True)
            await self._verify_password(user.id, password)
            existing = await self._uow.users.find_by_login(normalized)
            if existing is not None and existing.id != user.id:
                raise IdentityError(IdentityFailure.LOGIN_OCCUPIED)
            changed = user.change_login(normalized)
            if changed is not user:
                await self._uow.users.save(changed, user.revision)
            await self._uow.commit()
            return user_view(changed)

    async def change_password(
        self, actor: AuthenticatedActor, *, password: str, new_password: str
    ) -> None:
        """Сменить пароль и завершить остальные сессии.

        Args:
            actor: Проверенный владелец.
            password: Текущий пароль.
            new_password: Новый пароль без преобразований.

        Raises:
            IdentityError: Неверный текущий либо недопустимый новый пароль.
        """
        if not self._policy.accepts(new_password):
            raise IdentityError(IdentityFailure.INVALID_PASSWORD_LENGTH)
        now = self._clock.now()
        async with self._uow:
            user = await self._active_user(actor.user_id, lock=True)
            await self._verify_password(user.id, password)
            verifier = await self._passwords.encode(new_password)
            await self._uow.credentials.set(user.id, verifier, now)
            await self._uow.sessions.revoke_all(user.id, now, except_session_id=actor.session_id)
            await self._uow.commit()

    async def delete(self, actor: AuthenticatedActor, *, password: str, delete_media: bool) -> None:
        """Подтвердить внешний intent, затем атомарно обезличить аккаунт.

        Внешняя запись выполняется без SQL row locks. Подтверждённый intent
        можно воспроизвести после сбоя SQL и при восстановлении старой копии.

        Args:
            actor: Проверенный владелец.
            password: Текущий пароль.
            delete_media: Выбор удаления загруженных медиа.
        """
        async with self._uow:
            user = await self._active_user(actor.user_id)
            await self._verify_password(user.id, password)
        event = AccountDeleted(
            event_id=uuid4(),
            user_id=actor.user_id,
            delete_media=delete_media,
            occurred_at=self._clock.now(),
        )
        await self._journal.record(
            {
                "event_id": str(event.event_id),
                "kind": AccountEventKind.ACCOUNT,
                "user_id": str(event.user_id),
                "delete_media": event.delete_media,
                "occurred_at": event.occurred_at.isoformat(),
            }
        )
        await self.apply_recorded_deletion(event)

    async def apply_recorded_deletion(self, event: AccountDeleted) -> None:
        """Применить ранее авторизованное durable намерение удаления.

        Метод используется только после проверки пароля или чтения доверенного
        внешнего журнала административным процессом. Повторный intent не
        ослабляет уже выбранное удаление медиа.

        Args:
            event: Проверенное намерение из собственного журнала.
        """
        now = max(self._clock.now(), event.occurred_at)
        async with self._uow:
            user = await self._uow.users.get(event.user_id, lock=True)
            if user is None:
                deleted = User(
                    id=event.user_id,
                    login=None,
                    created_at=event.occurred_at,
                    state=UserState.DELETED,
                    roles=frozenset(),
                    deletion_requested_at=event.occurred_at,
                    deleted_at=now,
                    delete_media=event.delete_media,
                )
                await self._uow.users.save(deleted, None)
            else:
                if user.state is UserState.DELETED:
                    deleted = user.confirm_deletion_choice(delete_media=event.delete_media)
                else:
                    deleted = (
                        user.begin_deletion(now=now, delete_media=event.delete_media)
                        .confirm_deletion_choice(delete_media=event.delete_media)
                        .finish_deletion(now=now)
                    )
                if deleted is not user:
                    await self._uow.users.save(deleted, user.revision)
            await self._uow.credentials.delete(event.user_id)
            await self._uow.sessions.delete(event.user_id)
            await self._uow.outbox.add(event)
            await self._uow.commit()

    async def replay_deletion_journal(self) -> None:
        """Воспроизвести account intents до открытия восстановленного API.

        Raises:
            IdentityError: Журнал содержит неверный контракт аккаунта.
        """
        async for payload in self._journal.records():
            if payload.get("kind") != AccountEventKind.ACCOUNT:
                continue
            user_id = payload.get("user_id")
            event_id = payload.get("event_id")
            occurred_at = payload.get("occurred_at")
            delete_media = payload.get("delete_media")
            if (
                not isinstance(user_id, str)
                or not isinstance(event_id, str)
                or not isinstance(occurred_at, str)
                or not isinstance(delete_media, bool)
            ):
                raise IdentityError(IdentityFailure.INVALID_REQUEST)
            await self.apply_recorded_deletion(
                AccountDeleted(
                    event_id=UUID(event_id),
                    user_id=UserId(UUID(user_id)),
                    delete_media=delete_media,
                    occurred_at=datetime.fromisoformat(occurred_at),
                )
            )

    async def change_access(
        self,
        actor: AuthenticatedActor,
        *,
        user_id: UserId,
        blocked: bool,
        roles: frozenset[UserRole],
    ) -> UserView:
        """Применить операторское изменение ролей и блокировки.

        Args:
            actor: Проверенный оператор.
            user_id: Изменяемый аккаунт.
            blocked: Требуется ли блокировка.
            roles: Итоговые роли аккаунта.

        Returns:
            UserView: Обновлённое безопасное представление.

        Raises:
            IdentityError: Недостаточно прав либо аккаунт отсутствует.
        """
        async with self._uow:
            locked = {
                identifier: await self._uow.users.get(identifier, lock=True)
                for identifier in sorted(
                    {actor.user_id, user_id}, key=lambda identifier: identifier.int
                )
            }
            operator = locked[actor.user_id]
            if operator is None or operator.state is not UserState.ACTIVE:
                raise IdentityError(IdentityFailure.UNAUTHORIZED)
            if UserRole.OPERATOR not in operator.roles:
                raise IdentityError(IdentityFailure.FORBIDDEN)
            target = locked[user_id]
            if target is None:
                raise IdentityError(IdentityFailure.NOT_FOUND)
            changed = target.change_access(blocked=blocked, roles=roles)
            if changed is not target:
                await self._uow.users.save(changed, target.revision)
                await self._uow.sessions.revoke_all(target.id, self._clock.now())
            await self._uow.commit()
            return user_view(changed)

    async def _active_user(self, user_id: UserId, *, lock: bool = False) -> User:
        """Потребовать актуальный активный аккаунт.

        Args:
            user_id: Владелец операции.
            lock: Требуется ли блокировка транзакции.

        Returns:
            User: Активный аккаунт.

        Raises:
            IdentityError: Аккаунт не может выполнять операцию.
        """
        user = await self._uow.users.get(user_id, lock=lock)
        if user is None or user.state is not UserState.ACTIVE:
            raise IdentityError(IdentityFailure.UNAUTHORIZED)
        return user

    async def _verify_password(self, user_id: UserId, password: str) -> None:
        """Проверить текущий пароль владельца.

        Args:
            user_id: Владелец операции.
            password: Исходный пароль.

        Raises:
            IdentityError: Пароль неверен.
        """
        verifier = await self._uow.credentials.get(user_id)
        if not await self._passwords.verify(password, verifier):
            raise IdentityError(IdentityFailure.INVALID_CREDENTIALS)
