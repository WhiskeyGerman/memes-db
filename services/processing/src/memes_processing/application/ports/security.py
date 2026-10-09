"""Порт учетных данных доверенных устройств."""

from abc import ABC, abstractmethod


class IDeviceCredentials(ABC):
    """Генерация и проверка индивидуального секрета."""

    @abstractmethod
    def issue(self) -> tuple[str, str]:
        """Вернуть секрет и необратимый отпечаток для хранения."""

    @abstractmethod
    def verify(self, token: str, token_hash: str) -> bool:
        """Проверить отпечаток без утечки времени сравнения."""
