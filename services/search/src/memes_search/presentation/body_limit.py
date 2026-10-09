"""Ограничение HTTP-потока до создания multipart временных файлов."""

from enum import IntEnum, StrEnum
from http import HTTPMethod, HTTPStatus
from secrets import compare_digest
from typing import cast

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from memes_search.application.dto.queries import SearchLimit
from memes_search.infrastructure.configuration import Settings


class BodyLimit(IntEnum):
    """Ограниченная вместимость транспортных запросов."""

    JSON_BYTES = 131_072
    FEATURE_BYTES = 67_108_864
    MULTIPART_OVERHEAD_BYTES = 1_048_576


class BodyError(StrEnum):
    """Публичная причина отказа без содержимого файла."""

    TOO_LARGE = "request_body_too_large"


class BodyTooLargeError(HTTPException):
    """Внутренний сигнал превышения лимита транспортного потока."""

    def __init__(self) -> None:
        """Выдать явный отказ до разбора слишком большого тела."""
        super().__init__(
            status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE, detail=BodyError.TOO_LARGE
        )


class BodyLimitMiddleware:
    """Контроль каждого ASGI-чанка независимо от Content-Length."""

    def __init__(self, app: ASGIApp) -> None:
        """Сохранить типизированное ASGI-приложение без собственного состояния."""
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Отклонить большой поток до того, как parser заполнит диск."""
        maximum = (
            SearchLimit.MAXIMUM_SAMPLE_BYTES + BodyLimit.MULTIPART_OVERHEAD_BYTES
            if scope.get("path") == "/v1/search/samples" and scope.get("method") == HTTPMethod.POST
            else BodyLimit.JSON_BYTES
        )
        if scope.get("method") == HTTPMethod.POST and scope.get("path") in {
            "/internal/search/features",
            "/internal/search/sample-features",
        }:
            application = cast(FastAPI, scope["app"])
            settings = cast(Settings | None, getattr(application.state, "settings", None))
            provided = next(
                (
                    value
                    for name, value in scope.get("headers", ())
                    if name.lower() == b"x-internal-key"
                ),
                None,
            )
            if (
                settings is not None
                and provided is not None
                and compare_digest(provided, settings.internal_api_key.encode())
            ):
                maximum = BodyLimit.FEATURE_BYTES
        length = next(
            (
                value
                for name, value in scope.get("headers", ())
                if name.lower() == b"content-length"
            ),
            None,
        )
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                declared = BodyLimit.JSON_BYTES
            if declared > maximum:
                await self._reject(scope, receive, send)
                return
        total = 0

        async def bounded_receive() -> Message:
            """Проверить фактически полученные байты chunked-запроса."""
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > maximum:
                raise BodyTooLargeError()
            return message

        try:
            await self._app(scope, bounded_receive, send)
        except BodyTooLargeError:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE, content={"code": BodyError.TOO_LARGE}
        )
        await response(scope, receive, send)
