"""Публичная и межсервисная авторизация контроллеров Search."""

from hmac import compare_digest
from http import HTTPStatus
from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request

from memes_search.application.dto.identity import Principal
from memes_search.presentation.container import Services


def get_services(request: Request) -> Services:
    """Получить прикладные сценарии из composition root текущего процесса."""
    return cast(Services, request.app.state.runtime.services)


async def optional_principal(
    services: Annotated[Services, Depends(get_services)],
    authorization: Annotated[str | None, Header()] = None,
) -> Principal | None:
    """Разрешить гостевой поиск, отклоняя недействительные bearer-токены."""
    if authorization is None:
        return None
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(HTTPStatus.UNAUTHORIZED, detail="invalid_authorization")
    principal = await services.identity.introspect(parts[1])
    if principal is None:
        raise HTTPException(HTTPStatus.UNAUTHORIZED, detail="invalid_token")
    return principal


async def required_owner(
    principal: Annotated[Principal | None, Depends(optional_principal)],
) -> UUID:
    """Потребовать активную зарегистрированную учётную запись."""
    if principal is None:
        raise HTTPException(HTTPStatus.UNAUTHORIZED, detail="authentication_required")
    return principal.user_id


def internal_access(
    request: Request,
    internal_key: Annotated[str | None, Header(alias="X-Internal-Key")] = None,
) -> None:
    """Подтвердить сервисный доступ сравнением секрета в постоянное время."""
    expected = cast(str, request.app.state.settings.internal_api_key)
    if internal_key is None or not compare_digest(internal_key, expected):
        raise HTTPException(HTTPStatus.UNAUTHORIZED, detail="invalid_internal_key")
