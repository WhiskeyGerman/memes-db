"""Проверка bearer-токена через авторитетный Identity REST."""

from http import HTTPMethod, HTTPStatus
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from memes_platform.application.errors import ApplicationError
from memes_platform.infrastructure.http import InternalHttpClient
from memes_search.application.dto.identity import Principal
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.application.ports.identity import IIdentityReader


class IntrospectionResponse(BaseModel):
    """Типизированная форма активного субъекта."""

    user_id: UUID


class IdentityRestReader(IIdentityReader):
    """Порт проверки доступа без локального декодирования JWT."""

    def __init__(self, client: InternalHttpClient) -> None:
        """Сохранить ограниченный REST-клиент Identity."""
        self._client = client

    async def introspect(self, token: str) -> Principal | None:
        """Подтвердить действующую сессию, включая отзыв и блокировку."""
        try:
            body = await self._client.request(
                HTTPMethod.POST, "/internal/auth/introspect", json={"token": token}
            )
            return Principal(user_id=IntrospectionResponse.model_validate(body).user_id)
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
                return None
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error
        except (ApplicationError, ValidationError) as error:
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error
