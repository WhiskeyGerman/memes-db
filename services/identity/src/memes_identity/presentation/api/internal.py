"""Доверенные REST-контракты introspection и разрешения авторства."""

from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends

from memes_identity.application.use_cases.accounts import AccountUseCases
from memes_identity.application.use_cases.authentication import AuthenticationUseCases
from memes_identity.domain.user import UserId
from memes_identity.presentation.api.authentication import public_user
from memes_identity.presentation.api.security import internal_authorization
from memes_identity.presentation.dependencies import accounts, authentication
from memes_identity.presentation.schemas.authentication import (
    IntrospectionRequest,
    IntrospectionResponse,
    UserBatchRequest,
    UserBatchResponse,
)


class InternalPath(StrEnum):
    """Внутренние маршруты одного доверенного backend."""

    INTROSPECT = "/internal/auth/introspect"
    USERS = "/internal/users/batch"


router = APIRouter(dependencies=[Depends(internal_authorization)])


@router.post(InternalPath.INTROSPECT)
async def introspect(
    payload: IntrospectionRequest,
    use_cases: Annotated[AuthenticationUseCases, Depends(authentication)],
) -> IntrospectionResponse:
    """Проверить opaque token без разрешения подмены identity-заголовков.

    Args:
        payload: Непрозрачный секрет.
        use_cases: Прикладная авторизация.
    """
    actor = await use_cases.authenticate(payload.token.get_secret_value())
    return IntrospectionResponse(
        user_id=actor.user_id, login=actor.login, roles=sorted(actor.roles)
    )


@router.post(InternalPath.USERS)
async def authors(
    payload: UserBatchRequest,
    use_cases: Annotated[AccountUseCases, Depends(accounts)],
) -> UserBatchResponse:
    """Разрешить авторов с обезличиванием удалённых аккаунтов.

    Args:
        payload: Идентификаторы авторов.
        use_cases: Прикладные сценарии.
    """
    views = await use_cases.batch(tuple(UserId(user_id) for user_id in payload.user_ids))
    return UserBatchResponse(users=[public_user(view) for view in views])
