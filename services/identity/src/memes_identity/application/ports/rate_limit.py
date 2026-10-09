"""Порт распределённого ограничения попыток входа."""

from abc import ABC, abstractmethod
from datetime import datetime


class ILoginRateLimiter(ABC):
    """Атомарный ограничитель по IP и аккаунту во внешнем хранилище."""

    @abstractmethod
    async def consume(self, *, account: str, peer_ip: str, now: datetime) -> bool:
        """Учесть попытку и вернуть разрешение.

        Args:
            account: Предоставленный логин.
            peer_ip: IP сетевого клиента без недоверенных forwarded-заголовков.
            now: Серверное время.
        """
