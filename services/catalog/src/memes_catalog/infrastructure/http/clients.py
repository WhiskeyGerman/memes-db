"""REST-адаптеры Identity, Processing и Search."""

from enum import StrEnum
from http import HTTPMethod, HTTPStatus
from typing import cast
from uuid import UUID

import httpx

from memes_catalog.application.dto.catalog import (
    Actor,
    ActorRole,
    Author,
    CatalogDefault,
    SearchIndexTarget,
)
from memes_catalog.application.ports.resources import IIdentityClient, IRestDelivery
from memes_catalog.infrastructure.configuration import CatalogSettings
from memes_platform.application.dto.json import JsonObject, JsonValue
from memes_platform.application.errors import ApplicationError, ErrorCode
from memes_platform.infrastructure.http import InternalHttpClient


class RestOwner(StrEnum):
    """Постоянные получатели событий и выбранный ресурс чтения."""

    PROCESSING = "processing"
    SEARCH = "search"
    SHADOW_SEARCH = "search-shadow"


class IdentityClient(IIdentityClient):
    """Адаптер только опубликованного REST-контракта Identity."""

    def __init__(self, client: InternalHttpClient) -> None:
        """Сохранить ограниченный HTTP-клиент.

        Args:
            client: Транспорт с сервисным ключом.
        """
        self.client = client

    async def authenticate(self, token: str) -> Actor:
        """Проверить текущую сессию без локального кеша токена."""
        try:
            data = cast(
                JsonObject,
                await self.client.request(
                    HTTPMethod.POST, "/internal/auth/introspect", json={"token": token}
                ),
            )
        except httpx.HTTPStatusError as error:
            code = (
                ErrorCode.UNAUTHORIZED
                if error.response.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN)
                else ErrorCode.UNAVAILABLE
            )
            raise ApplicationError(code) from error
        return Actor(
            user_id=UUID(cast(str, data["user_id"])),
            roles=frozenset(ActorRole(str(role)) for role in cast(list[JsonValue], data["roles"])),
        )

    async def authors(self, user_ids: tuple[UUID, ...]) -> tuple[Author, ...]:
        """Загрузить текущие логины либо обезличенные аккаунты."""
        if not user_ids:
            return ()
        users: list[JsonObject] = []
        try:
            for offset in range(0, len(user_ids), CatalogDefault.MAX_PAGE_SIZE):
                data = cast(
                    JsonObject,
                    await self.client.request(
                        HTTPMethod.POST,
                        "/internal/users/batch",
                        json={
                            "user_ids": [
                                str(value)
                                for value in user_ids[
                                    offset : offset + CatalogDefault.MAX_PAGE_SIZE
                                ]
                            ]
                        },
                    ),
                )
                users.extend(cast(list[JsonObject], data["users"]))
        except httpx.HTTPStatusError as error:
            raise ApplicationError(ErrorCode.UNAVAILABLE) from error
        return tuple(
            Author(
                user_id=UUID(cast(str, user["user_id"])),
                login=cast(str | None, user.get("login")),
                delete_media=cast(bool, user.get("delete_media", False)),
            )
            for user in users
        )


class RestDelivery(IRestDelivery):
    """Предопределённые получатели REST outbox."""

    def __init__(self, settings: CatalogSettings) -> None:
        """Создать клиентов конкретных ресурсов.

        Args:
            settings: Конфигурация релиза.
        """
        self.suggestions_owner = (
            RestOwner.SHADOW_SEARCH
            if settings.active_search_index is SearchIndexTarget.SHADOW
            else RestOwner.SEARCH
        )
        self.clients: dict[str, InternalHttpClient] = {
            RestOwner.PROCESSING: InternalHttpClient(
                base_url=settings.processing_url,
                internal_api_key=settings.internal_api_key,
                timeout=settings.http_timeout_seconds,
            ),
            RestOwner.SEARCH: InternalHttpClient(
                base_url=settings.search_url,
                internal_api_key=settings.internal_api_key,
                timeout=settings.http_timeout_seconds,
            ),
        }

        if settings.shadow_search_url is not None:
            self.clients[RestOwner.SHADOW_SEARCH] = InternalHttpClient(
                base_url=settings.shadow_search_url,
                internal_api_key=settings.internal_api_key,
                timeout=settings.http_timeout_seconds,
            )

    async def deliver(self, target: str, payload: JsonObject) -> None:
        """Доставить сохранённую команду только разрешённому получателю."""
        owner, path = target.split(":", maxsplit=1)
        if owner not in self.clients or not path.startswith("/internal/"):
            raise ApplicationError(ErrorCode.INVALID_INPUT)
        try:
            await self.clients[owner].request(HTTPMethod.POST, path, json=payload)
        except httpx.HTTPStatusError as error:
            raise ApplicationError(ErrorCode.UNAVAILABLE) from error

    async def pending_tasks(self) -> int:
        """Проверить доступность и backlog координатора."""
        try:
            data = cast(
                JsonObject,
                await self.clients[RestOwner.PROCESSING].request(
                    HTTPMethod.GET, "/internal/status"
                ),
            )
        except httpx.HTTPStatusError as error:
            raise ApplicationError(ErrorCode.UNAVAILABLE) from error
        return cast(int, data["pending_publication_tasks"])

    async def suggestions(self, media_id: UUID, *, limit: int) -> JsonObject:
        """Получить кандидатов вариаций по опубликованным векторам.

        Args:
            media_id: Исходная публикация.
            limit: Максимум кандидатов.
        """
        try:
            return cast(
                JsonObject,
                await self.clients[self.suggestions_owner].request(
                    HTTPMethod.POST,
                    "/internal/search/suggestions",
                    json={"media_id": str(media_id), "limit": limit},
                ),
            )
        except httpx.HTTPStatusError as error:
            raise ApplicationError(ErrorCode.UNAVAILABLE) from error

    async def close(self) -> None:
        """Закрыть все транспортные пулы процесса."""
        for client in self.clients.values():
            await client.close()
