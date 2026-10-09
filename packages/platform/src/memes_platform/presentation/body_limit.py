"""Ограничение недоверенного HTTP-потока до построения модели запроса."""

from enum import IntEnum, StrEnum
from http import HTTPStatus
from secrets import compare_digest

from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class RequestLimit(IntEnum):
    """Предельный размер ограниченных JSON-контрактов."""

    PUBLIC_BYTES = 131072
    INTERNAL_BYTES = 67108864


class RequestSetting(StrEnum):
    """Предопределённая граница доверенного потока."""

    INTERNAL_PREFIX = "/internal/"
    HEADER = "x-internal-key"
    CONTENT_LENGTH = "content-length"
    TOO_LARGE = "request_body_too_large"


class RequestBodyTooLargeError(HTTPException):
    """Сигнал превышения фактического числа байтов."""

    def __init__(self) -> None:
        """Сохранить конечный транспортный отказ для parser FastAPI."""
        super().__init__(
            status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE, detail=RequestSetting.TOO_LARGE
        )


class RequestBodyLimitMiddleware:
    """Проверка Content-Length и chunked независимо от parser FastAPI."""

    def __init__(self, app: ASGIApp) -> None:
        """Сохранить ASGI-границу.

        Args:
            app: HTTP-приложение.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Ограничить обычный JSON и отдельно доверенный REST callback."""
        maximum = RequestLimit.PUBLIC_BYTES
        headers = dict(scope.get("headers", []))
        application = scope.get("app")
        expected = getattr(getattr(application, "state", None), "internal_api_key", None)
        provided = headers.get(RequestSetting.HEADER.encode())
        if (
            scope.get("path", "").startswith(RequestSetting.INTERNAL_PREFIX)
            and isinstance(expected, str)
            and provided is not None
            and compare_digest(provided, expected.encode())
        ):
            maximum = RequestLimit.INTERNAL_BYTES
        length = headers.get(RequestSetting.CONTENT_LENGTH.encode())
        if length is not None:
            try:
                declared = int(length)
            except ValueError:
                declared = maximum + 1
            if declared > maximum or declared < 0:
                await self.reject(scope, receive, send)
                return
        total = 0
        started = False

        async def bounded_receive() -> Message:
            """Ограничить поток, даже если Content-Length отсутствует."""
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > maximum:
                raise RequestBodyTooLargeError()
            return message

        async def tracked_send(message: Message) -> None:
            """Не допустить второго ответа после начала передачи."""
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, tracked_send)
        except RequestBodyTooLargeError:
            if started:
                raise
            await self.reject(scope, receive, send)

    @staticmethod
    async def reject(scope: Scope, receive: Receive, send: Send) -> None:
        """Вернуть безопасный отказ без содержимого запроса."""
        await JSONResponse(
            status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            content={"code": RequestSetting.TOO_LARGE},
        )(scope, receive, send)
