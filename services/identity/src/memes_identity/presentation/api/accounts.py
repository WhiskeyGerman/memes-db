"""HTTP-адаптер изменения аккаунта и операторского доступа."""

from enum import StrEnum
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response

from memes_identity.application.dto.authentication import AuthenticatedActor
from memes_identity.application.use_cases.accounts import AccountUseCases
from memes_identity.domain.user import UserId
from memes_identity.presentation.api.authentication import public_user
from memes_identity.presentation.api.security import clear_token_cookies
from memes_identity.presentation.dependencies import accounts, authenticated_actor
from memes_identity.presentation.schemas.authentication import (
    ChangeAccessRequest,
    ChangeLoginRequest,
    ChangePasswordRequest,
    DeleteAccountRequest,
    UserResponse,
)


class AccountPath(StrEnum):
    """Публичные маршруты аккаунта."""

    LOGIN = "/v1/auth/login"
    PASSWORD = "/v1/auth/password"  # noqa: S105 - имя маршрута, не секрет.
    ACCOUNT = "/v1/auth/account"
    ACCESS = "/v1/operator/users/{user_id}/access"


router = APIRouter()


@router.patch(AccountPath.LOGIN)
async def change_login(
    payload: ChangeLoginRequest,
    actor: Annotated[AuthenticatedActor, Depends(authenticated_actor)],
    use_cases: Annotated[AccountUseCases, Depends(accounts)],
) -> UserResponse:
    """Сменить логин с подтверждением текущего пароля.

    Args:
        payload: Новый логин и подтверждение.
        actor: Проверенный владелец.
        use_cases: Прикладные сценарии.
    """
    return public_user(
        await use_cases.change_login(
            actor, login=payload.login, password=payload.password.get_secret_value()
        )
    )


@router.patch(AccountPath.PASSWORD, status_code=HTTPStatus.NO_CONTENT)
async def change_password(
    payload: ChangePasswordRequest,
    actor: Annotated[AuthenticatedActor, Depends(authenticated_actor)],
    use_cases: Annotated[AccountUseCases, Depends(accounts)],
) -> None:
    """Сменить пароль и завершить остальные сессии.

    Args:
        payload: Старый и новый пароли.
        actor: Проверенный владелец.
        use_cases: Прикладные сценарии.
    """
    await use_cases.change_password(
        actor,
        password=payload.password.get_secret_value(),
        new_password=payload.new_password.get_secret_value(),
    )


@router.delete(AccountPath.ACCOUNT, status_code=HTTPStatus.NO_CONTENT)
async def delete_account(
    payload: DeleteAccountRequest,
    response: Response,
    actor: Annotated[AuthenticatedActor, Depends(authenticated_actor)],
    use_cases: Annotated[AccountUseCases, Depends(accounts)],
) -> None:
    """Обезличить аккаунт с выбором удаления собственных медиа.

    Args:
        payload: Подтверждение и выбор пользователя.
        response: Ответ для очистки cookie.
        actor: Проверенный владелец.
        use_cases: Прикладные сценарии.
    """
    await use_cases.delete(
        actor, password=payload.password.get_secret_value(), delete_media=payload.delete_media
    )
    clear_token_cookies(response)


@router.patch(AccountPath.ACCESS)
async def change_access(
    user_id: UUID,
    payload: ChangeAccessRequest,
    actor: Annotated[AuthenticatedActor, Depends(authenticated_actor)],
    use_cases: Annotated[AccountUseCases, Depends(accounts)],
) -> UserResponse:
    """Изменить доступ аккаунта от имени оператора.

    Args:
        user_id: Изменяемый аккаунт.
        payload: Итоговое состояние доступа.
        actor: Проверенный оператор.
        use_cases: Прикладной сценарий.
    """
    return public_user(
        await use_cases.change_access(
            actor, user_id=UserId(user_id), blocked=payload.blocked, roles=frozenset(payload.roles)
        )
    )
