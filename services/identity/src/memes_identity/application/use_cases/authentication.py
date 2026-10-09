"""Прикладные сценарии входа и управления сессиями."""

from uuid import uuid4

from memes_identity.application.dto.authentication import (
    AuthenticatedActor,
    IssuedTokens,
    PasswordPolicy,
)
from memes_identity.application.errors import IdentityError, IdentityFailure
from memes_identity.application.ports.clock import IClock
from memes_identity.application.ports.passwords import IPasswordVerifier
from memes_identity.application.ports.unit_of_work import IIdentityUnitOfWork
from memes_identity.domain.user import Login, User, UserId, UserState
from memes_kernel.errors import DomainError


class AuthenticationUseCases:
    """Оркестрация авторизации без знания HTTP и способа хранения токенов."""

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
            uow: Атомарная транзакция.
            passwords: Проверка паролей.
            clock: Источник серверного времени.
            policy: Политика длины пароля.
        """
        self._uow = uow
        self._passwords = passwords
        self._clock = clock
        self._policy = policy

    async def register(self, *, login: str, password: str, peer_ip: str) -> IssuedTokens:
        """Зарегистрировать аккаунт и открыть первую сессию.

        Args:
            login: Исходный логин.
            password: Исходный пароль.
            peer_ip: Адрес клиента для ограничения запросов.

        Returns:
            IssuedTokens: Токены новой сессии.

        Raises:
            IdentityError: Логин занят, пароль недопустим либо превышен лимит.
        """
        normalized = Login(value=login)
        if not self._policy.accepts(password):
            raise IdentityError(IdentityFailure.INVALID_PASSWORD_LENGTH)
        now = self._clock.now()
        async with self._uow:
            allowed = await self._uow.rate_limiter.consume(
                account=normalized.value, peer_ip=peer_ip, now=now
            )
            if not allowed:
                await self._uow.commit()
                raise IdentityError(IdentityFailure.RATE_LIMITED)
            if await self._uow.users.find_by_login(normalized) is not None:
                await self._uow.commit()
                raise IdentityError(IdentityFailure.LOGIN_OCCUPIED)
            verifier = await self._passwords.encode(password)
            user = User(id=UserId(uuid4()), login=normalized, created_at=now)
            await self._uow.users.save(user, expected_revision=None)
            await self._uow.credentials.set(user.id, verifier, now)
            tokens = await self._uow.sessions.issue(user.id, now)
            await self._uow.commit()
            return tokens

    async def login(self, *, login: str, password: str, peer_ip: str) -> IssuedTokens:
        """Проверить пароль без раскрытия существования логина.

        Args:
            login: Предоставленный логин.
            password: Предоставленный пароль.
            peer_ip: Сетевой адрес клиента.

        Returns:
            IssuedTokens: Токены новой сессии.

        Raises:
            IdentityError: Единый отказ аутентификации либо превышенный лимит.
        """
        now = self._clock.now()
        try:
            normalized = Login(value=login)
        except DomainError:
            normalized = None
        async with self._uow:
            allowed = await self._uow.rate_limiter.consume(
                account=login.lower(), peer_ip=peer_ip, now=now
            )
            if not allowed:
                await self._uow.commit()
                raise IdentityError(IdentityFailure.RATE_LIMITED)
            user = (
                await self._uow.users.find_by_login(normalized) if normalized is not None else None
            )
            verifier = await self._uow.credentials.get(user.id) if user is not None else None
            valid = await self._passwords.verify(password, verifier)
            if not valid or user is None or user.state is not UserState.ACTIVE:
                await self._uow.commit()
                raise IdentityError(IdentityFailure.INVALID_CREDENTIALS)
            current = await self._uow.users.get(user.id, lock=True)
            if (
                current is None
                or current.state is not UserState.ACTIVE
                or await self._uow.credentials.get(current.id) != verifier
            ):
                await self._uow.commit()
                raise IdentityError(IdentityFailure.INVALID_CREDENTIALS)
            tokens = await self._uow.sessions.issue(current.id, now)
            await self._uow.commit()
            return tokens

    async def refresh(self, refresh_token: str) -> IssuedTokens:
        """Ротировать токен; зафиксировать отзыв семейства перед отказом replay.

        Args:
            refresh_token: Секрет из защищённого cookie.

        Returns:
            IssuedTokens: Новая пара токенов.

        Raises:
            IdentityError: Токен недействителен либо повторно использован.
        """
        async with self._uow:
            result = await self._uow.sessions.rotate(refresh_token, self._clock.now())
            await self._uow.commit()
        if result.tokens is None:
            raise IdentityError(result.failure or IdentityFailure.UNAUTHORIZED)
        return result.tokens

    async def authenticate(self, access_token: str) -> AuthenticatedActor:
        """Проверить сессию и актуальное состояние пользователя.

        Args:
            access_token: Непрозрачный access token.

        Returns:
            AuthenticatedActor: Проверенный владелец текущей сессии.

        Raises:
            IdentityError: Сессия либо аккаунт недействительны.
        """
        async with self._uow:
            actor = await self._uow.sessions.authenticate(access_token, self._clock.now())
        if actor is None:
            raise IdentityError(IdentityFailure.UNAUTHORIZED)
        return actor

    async def logout(self, actor: AuthenticatedActor, *, all_sessions: bool = False) -> None:
        """Отозвать текущую либо все сессии аккаунта.

        Args:
            actor: Проверенный участник.
            all_sessions: Отозвать все семейства владельца.
        """
        now = self._clock.now()
        async with self._uow:
            if all_sessions:
                await self._uow.users.get(actor.user_id, lock=True)
                await self._uow.sessions.revoke_all(actor.user_id, now)
            else:
                await self._uow.sessions.revoke(actor.session_id, now)
            await self._uow.commit()
