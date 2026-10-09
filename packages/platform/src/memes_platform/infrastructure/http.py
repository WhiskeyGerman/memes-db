"""HTTP-транспорт межсервисных адаптеров с явным сроком ожидания."""

from enum import StrEnum
from http import HTTPMethod
from json import JSONDecodeError
from typing import TypeAlias, cast

import httpx

from memes_platform.application.errors import ApplicationError, ErrorCode
from memes_platform.infrastructure.configuration import RuntimeDefault
from memes_platform.infrastructure.correlation import request_id

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class InternalHeader(StrEnum):
    """Заголовки доверенного транспорта."""

    AUTHENTICATION = "X-Internal-Key"
    REQUEST_ID = "X-Request-ID"


class InternalHttpClient:
    """Ограниченный клиент, которым владеет один процесс."""

    def __init__(
        self,
        *,
        base_url: str,
        internal_api_key: str,
        timeout: float = RuntimeDefault.HTTP_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Создать пул соединений.

        Args:
            base_url: Адрес ресурса из окружения.
            internal_api_key: Секрет сервисного доступа.
            timeout: Конечный срок запроса.
            transport: Явный адаптер HTTP для изолированного contract test.
        """
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={InternalHeader.AUTHENTICATION.value: internal_api_key},
            follow_redirects=False,
            transport=transport,
        )

    async def request(self, method: HTTPMethod, path: str, *, json: JsonValue = None) -> JsonValue:
        """Выполнить запрос; HTTP-ошибки обрабатывает конкретный адаптер.

        Args:
            method: HTTP-метод.
            path: Проверенный путь API.
            json: Транспортный payload.
        """
        try:
            identifier = request_id.get()
            response = await self._client.request(
                method,
                path,
                json=json,
                headers={} if identifier is None else {InternalHeader.REQUEST_ID.value: identifier},
            )
            response.raise_for_status()
            return cast(JsonValue, response.json())
        except (httpx.TransportError, httpx.DecodingError, JSONDecodeError) as error:
            raise ApplicationError(ErrorCode.UNAVAILABLE) from error

    async def close(self) -> None:
        """Закрыть соединения при graceful shutdown."""
        await self._client.aclose()
