"""Composition-зависимости HTTP-адаптера Identity."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, cast

from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_identity.application.dto.authentication import AuthenticatedActor, PasswordPolicy
from memes_identity.application.errors import IdentityError, IdentityFailure
from memes_identity.application.ports.clock import IClock
from memes_identity.application.ports.passwords import IPasswordVerifier
from memes_identity.application.use_cases.accounts import AccountUseCases
from memes_identity.application.use_cases.authentication import AuthenticationUseCases
from memes_identity.infrastructure.configuration import IdentitySettings
from memes_identity.infrastructure.persistence.unit_of_work import SqlIdentityUnitOfWork
from memes_platform.application.ports.deletion_journal import IDeletionJournal


class AuthorizationSyntax(StrEnum):
    """Синтаксис предъявления токена."""

    BEARER = "bearer"
    SEPARATOR = " "


@dataclass(frozen=True, slots=True, kw_only=True)
class IdentityContainer:
    """Зависимости процесса; существенное состояние остаётся в PostgreSQL."""

    settings: IdentitySettings
    factory: async_sessionmaker[AsyncSession]
    passwords: IPasswordVerifier
    clock: IClock
    journal: IDeletionJournal

    def authentication(self) -> AuthenticationUseCases:
        """Создать независимый прикладной сценарий HTTP-запроса."""
        return AuthenticationUseCases(
            uow=SqlIdentityUnitOfWork(self.factory, self.settings),
            passwords=self.passwords,
            clock=self.clock,
            policy=PasswordPolicy(),
        )

    def accounts(self) -> AccountUseCases:
        """Создать независимый сценарий изменения аккаунта."""
        return AccountUseCases(
            uow=SqlIdentityUnitOfWork(self.factory, self.settings),
            passwords=self.passwords,
            clock=self.clock,
            policy=PasswordPolicy(),
            journal=self.journal,
        )


def container(request: Request) -> IdentityContainer:
    """Получить композицию зависимостей текущего процесса.

    Args:
        request: HTTP-запрос.
    """
    return cast(IdentityContainer, request.app.state.identity)


def authentication(
    dependencies: Annotated[IdentityContainer, Depends(container)],
) -> AuthenticationUseCases:
    """Получить сценарии авторизации.

    Args:
        dependencies: Composition-контейнер.
    """
    return dependencies.authentication()


def accounts(
    dependencies: Annotated[IdentityContainer, Depends(container)],
) -> AccountUseCases:
    """Получить сценарии аккаунта.

    Args:
        dependencies: Composition-контейнер.
    """
    return dependencies.accounts()


async def authenticated_actor(
    use_cases: Annotated[AuthenticationUseCases, Depends(authentication)],
    authorization: Annotated[str | None, Header()] = None,
) -> AuthenticatedActor:
    """Проверить Bearer и текущее состояние аккаунта.

    Args:
        use_cases: Прикладная авторизация.
        authorization: Недоверенный заголовок клиента.

    Raises:
        IdentityError: Заголовок отсутствует либо токен недействителен.
    """
    if authorization is None:
        raise IdentityError(IdentityFailure.UNAUTHORIZED)
    scheme, separator, token = authorization.partition(AuthorizationSyntax.SEPARATOR)
    if scheme.lower() != AuthorizationSyntax.BEARER or not separator or not token:
        raise IdentityError(IdentityFailure.UNAUTHORIZED)
    return await use_cases.authenticate(token)
