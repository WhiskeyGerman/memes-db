"""Порт проверенной пользовательской идентичности."""

from abc import ABC, abstractmethod

from memes_processing.application.dto.identity import Principal


class IIdentityGateway(ABC):
    """Сервис владеет лишь разрешениями, Identity владеет аутентификацией."""

    @abstractmethod
    async def principal(self, token: str) -> Principal:
        """Проверить непрозрачный access token у его владельца."""
