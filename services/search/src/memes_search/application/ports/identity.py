"""Порт проверки доступа к приватным образцам."""

from abc import ABC, abstractmethod

from memes_search.application.dto.identity import Principal


class IIdentityReader(ABC):
    """Подтверждение личности через внешний identity-ресурс."""

    @abstractmethod
    async def introspect(self, token: str) -> Principal | None:
        """Вернуть активного субъекта либо недействительный токен."""
