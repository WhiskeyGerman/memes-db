"""Публичные HTTP-маршруты аутентификации."""

from enum import StrEnum
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from memes_identity.application.dto.authentication import AuthenticatedActor, UserView
from memes_identity.application.use_cases.accounts import AccountUseCases
from memes_identity.application.use_cases.authentication import AuthenticationUseCases
from memes_identity.presentation.api.security import (
    clear_token_cookies,
    refresh_cookie,
    require_origin,
    set_token_cookies,
)
from memes_identity.presentation.dependencies import (
    IdentityContainer,
    accounts,
    authenticated_actor,
    authentication,
    container,
)
from memes_identity.presentation.schemas.authentication import (
    AccessResponse,
    CredentialsRequest,
    UserResponse,
)


class AuthenticationPath(StrEnum):
    """Внешние маршруты авторизации."""

    PREFIX = "/v1/auth"
    REGISTER = "/register"
    LOGIN = "/login"
    REFRESH = "/refresh"
    LOGOUT = "/logout"
    LOGOUT_ALL = "/logout-all"
    ME = "/me"


router = APIRouter(prefix=AuthenticationPath.PREFIX)


def public_user(view: UserView) -> UserResponse:
    """Сериализовать безопасную проекцию пользователя.

    Args:
        view: Прикладная проекция.

    Returns:
        UserResponse: Внешний JSON-контракт.
    """
    return UserResponse(
        user_id=view.user_id,
        login=view.login,
        roles=sorted(view.roles),
        state=view.state,
        delete_media=view.delete_media,
    )


def peer_ip(request: Request) -> str:
    """Использовать реальный peer, не доверяя клиентскому forwarded header.

    Args:
        request: HTTP-запрос.
    """
    return request.client.host if request.client is not None else "unknown"


@router.post(AuthenticationPath.REGISTER, status_code=HTTPStatus.CREATED)
async def register(
    payload: CredentialsRequest,
    request: Request,
    response: Response,
    use_cases: Annotated[AuthenticationUseCases, Depends(authentication)],
    dependencies: Annotated[IdentityContainer, Depends(container)],
) -> AccessResponse:
    """Зарегистрировать аккаунт и установить защищённые cookie.

    Args:
        payload: Логин и исходный пароль.
        request: HTTP-запрос.
        response: Ответ для cookie.
        use_cases: Прикладные сценарии.
        dependencies: Composition-конфигурация.
    """
    require_origin(request, dependencies)
    tokens = await use_cases.register(
        login=payload.login, password=payload.password.get_secret_value(), peer_ip=peer_ip(request)
    )
    set_token_cookies(response, tokens, dependencies)
    return AccessResponse(
        access_token=tokens.access_token, access_expires_at=tokens.access_expires_at
    )


@router.post(AuthenticationPath.LOGIN)
async def login(
    payload: CredentialsRequest,
    request: Request,
    response: Response,
    use_cases: Annotated[AuthenticationUseCases, Depends(authentication)],
    dependencies: Annotated[IdentityContainer, Depends(container)],
) -> AccessResponse:
    """Войти с одинаковой ошибкой неизвестного логина и неверного пароля.

    Args:
        payload: Предоставленные credentials.
        request: HTTP-запрос.
        response: Ответ для cookie.
        use_cases: Прикладная авторизация.
        dependencies: Конфигурация процесса.
    """
    require_origin(request, dependencies)
    tokens = await use_cases.login(
        login=payload.login, password=payload.password.get_secret_value(), peer_ip=peer_ip(request)
    )
    set_token_cookies(response, tokens, dependencies)
    return AccessResponse(
        access_token=tokens.access_token, access_expires_at=tokens.access_expires_at
    )


@router.post(AuthenticationPath.REFRESH)
async def refresh(
    request: Request,
    response: Response,
    use_cases: Annotated[AuthenticationUseCases, Depends(authentication)],
    dependencies: Annotated[IdentityContainer, Depends(container)],
) -> AccessResponse:
    """Ротировать cookie-сессию после CSRF-проверки.

    Args:
        request: Запрос с refresh и CSRF cookie.
        response: Ответ для новых cookie.
        use_cases: Прикладная авторизация.
        dependencies: Конфигурация процесса.
    """
    tokens = await use_cases.refresh(refresh_cookie(request, dependencies))
    set_token_cookies(response, tokens, dependencies)
    return AccessResponse(
        access_token=tokens.access_token, access_expires_at=tokens.access_expires_at
    )


@router.post(AuthenticationPath.LOGOUT, status_code=HTTPStatus.NO_CONTENT)
async def logout(
    response: Response,
    actor: Annotated[AuthenticatedActor, Depends(authenticated_actor)],
    use_cases: Annotated[AuthenticationUseCases, Depends(authentication)],
) -> None:
    """Завершить Bearer-сессию и очистить cookie.

    Args:
        response: HTTP-ответ.
        actor: Проверенный участник.
        use_cases: Прикладная авторизация.
    """
    await use_cases.logout(actor)
    clear_token_cookies(response)


@router.post(AuthenticationPath.LOGOUT_ALL, status_code=HTTPStatus.NO_CONTENT)
async def logout_all(
    response: Response,
    actor: Annotated[AuthenticatedActor, Depends(authenticated_actor)],
    use_cases: Annotated[AuthenticationUseCases, Depends(authentication)],
) -> None:
    """Завершить все сессии владельца.

    Args:
        response: HTTP-ответ.
        actor: Проверенный участник.
        use_cases: Прикладная авторизация.
    """
    await use_cases.logout(actor, all_sessions=True)
    clear_token_cookies(response)


@router.get(AuthenticationPath.ME)
async def me(
    actor: Annotated[AuthenticatedActor, Depends(authenticated_actor)],
    use_cases: Annotated[AccountUseCases, Depends(accounts)],
) -> UserResponse:
    """Получить данные текущего аккаунта.

    Args:
        actor: Проверенный участник.
        use_cases: Сценарии аккаунта.
    """
    return public_user(await use_cases.me(actor))
