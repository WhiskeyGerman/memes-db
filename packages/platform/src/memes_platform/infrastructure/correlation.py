"""Контекст одного запроса без хранения пользовательских секретов."""

from contextvars import ContextVar

request_id: ContextVar[str | None] = ContextVar("memes_request_id", default=None)
