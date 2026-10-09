"""Стабильная публичная адресация собственного поискового индекса."""

from enum import IntEnum, StrEnum
from typing import cast
from urllib.parse import unquote_to_bytes

from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from memes_search.application.dto.indexes import IndexTarget
from memes_search.presentation.container import Runtime
from memes_search.presentation.links import SearchResourceLinks


class AliasPath(StrEnum):
    """Публичный prefix без привязки к версии индекса."""

    PUBLIC = "/v1/search"


class EncodedPathLimit(IntEnum):
    """Вместимость percent-encoded символа URI."""

    ESCAPE_BYTES = 3


class IndexAliasMiddleware:
    """Переписать только собственный alias до проверки HTTP-тела."""

    def __init__(self, app: ASGIApp) -> None:
        """Сохранить следующий ASGI-обработчик."""
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Удержать ресурсы в собственном deployment без внешнего router."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        application = cast(FastAPI, scope["app"])
        runtime = cast(Runtime | None, getattr(application.state, "runtime", None))
        prefix = (
            runtime.services.links.prefix
            if runtime is not None
            else SearchResourceLinks.for_target(IndexTarget.PRIMARY).prefix
        )
        path = cast(str, scope.get("path", ""))
        if path != prefix and not path.startswith(f"{prefix}/"):
            await self._app(scope, receive, send)
            return
        rewritten: Scope = dict(scope)
        rewritten["path"] = f"{AliasPath.PUBLIC}{path[len(prefix) :]}"
        raw = cast(bytes | None, scope.get("raw_path"))
        if raw is not None:
            rewritten["raw_path"] = self._raw_path(raw, prefix, rewritten["path"])
        await self._app(rewritten, receive, send)

    @staticmethod
    def _raw_path(raw: bytes, prefix: str, rewritten: str) -> bytes:
        """Сохранить исходную кодировку suffix и неизменный query string.

        Args:
            raw: Исходный ASGI :attr:`raw_path`.
            prefix: Разрешённый prefix конкретного deployment.
            rewritten: Декодированный путь после переписывания.

        Returns:
            Путь с заменённым prefix и исходными байтами оставшегося URI.
        """
        encoded_prefix = prefix.encode()
        maximum = min(len(raw), len(encoded_prefix) * EncodedPathLimit.ESCAPE_BYTES)
        for boundary in range(len(encoded_prefix), maximum + 1):
            if unquote_to_bytes(raw[:boundary]) == encoded_prefix:
                return AliasPath.PUBLIC.encode() + raw[boundary:]
        return rewritten.encode()
