"""Выбор пользователя при удалении аккаунта."""

from dataclasses import dataclass
from enum import StrEnum

from memes_catalog.domain.media import AuthorId
from memes_kernel.values import require_identifier


class AuthoredMediaAction(StrEnum):
    """Политика обработки авторских публикаций."""

    KEEP = "keep"
    DELETE = "delete"


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountDeletionChoice:
    """Сохранённое волеизъявление владельца аккаунта.

    При :attr:`AuthoredMediaAction.KEEP` публикации сохраняют технический ID
    автора; публичная проекция показывает удалённый аккаунт.
    """

    author_id: AuthorId
    media_action: AuthoredMediaAction = AuthoredMediaAction.KEEP

    def __post_init__(self) -> None:
        """Проверить технический идентификатор автора."""
        require_identifier(self.author_id)
