"""Интерфейсы внешних ресурсов без SDK и транспорта."""

from abc import ABC, abstractmethod
from pathlib import Path
from uuid import UUID

from memes_catalog.application.dto.catalog import Actor, Author, InspectedMedia
from memes_catalog.domain.media import MediaFormat
from memes_kernel.values import FileRef
from memes_platform.application.dto.json import JsonObject


class IObjectStorage(ABC):
    """Порт ограниченного доступа к приватным объектам."""

    @abstractmethod
    async def upload_ticket(
        self, key: str, *, maximum_bytes: int, expires_seconds: int
    ) -> tuple[str, dict[str, str]]:
        """Выдать форму передачи.

        Args:
            key: Единственный разрешённый ключ.
            maximum_bytes: Предел размера.
            expires_seconds: Срок разрешения.
        """

    @abstractmethod
    async def download(self, key: str, destination: Path, *, maximum_bytes: int) -> FileRef:
        """Скачать объект с подсчётом размера и SHA-256.

        Args:
            key: Ключ оригинала.
            destination: Временный путь.
            maximum_bytes: Предел размера.
        """

    @abstractmethod
    async def copy(
        self, source: str, destination: str, *, expected_etag: str, media_format: MediaFormat
    ) -> None:
        """Копировать в постоянную область.

        Args:
            source: Временный ключ.
            destination: Новый уникальный ключ.
            expected_etag: Версия проверенного содержимого.
            media_format: Проверенный браузерный формат.
        """

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Идемпотентно удалить объект.

        Args:
            key: Ключ конкретной старой версии.
        """

    @abstractmethod
    async def download_url(self, key: str, *, expires_seconds: int) -> str:
        """Выдать краткую ссылку.

        Args:
            key: Ключ живой публикации.
            expires_seconds: Срок разрешения.
        """

    @abstractmethod
    async def ready(self) -> bool:
        """Проверить доступность подключённого bucket."""


class IMediaInspector(ABC):
    """Порт базового декодирования без доверия расширению."""

    @abstractmethod
    async def inspect_object(
        self, object_key: str, *, file_id: UUID, maximum_bytes: int
    ) -> InspectedMedia:
        """Проверить фактические байты объекта.

        Args:
            object_key: Ключ внутри хранилища.
            file_id: Идентичность файла.
            maximum_bytes: Предел чтения.
        """


class IIdentityClient(ABC):
    """Порт проверки доступа и текущего обезличивания."""

    @abstractmethod
    async def authenticate(self, token: str) -> Actor:
        """Проверить непрозрачный access token.

        Args:
            token: Секрет доступа.
        """

    @abstractmethod
    async def authors(self, user_ids: tuple[UUID, ...]) -> tuple[Author, ...]:
        """Получить публичные текущие атрибуты.

        Args:
            user_ids: Ограниченный список авторов.
        """


class IRestDelivery(ABC):
    """Порт доставки сохранённых событий и проверки очереди."""

    @abstractmethod
    async def deliver(self, target: str, payload: JsonObject) -> None:
        """Доставить одну идемпотентную команду.

        Args:
            target: Предопределённая цель.
            payload: Сохранённый пакет.
        """

    @abstractmethod
    async def suggestions(self, media_id: UUID, *, limit: int) -> JsonObject:
        """Получить ограниченный набор кандидатов.

        Args:
            media_id: Источник.
            limit: Максимум кандидатов.
        """

    @abstractmethod
    async def pending_tasks(self) -> int:
        """Получить размер очереди публикаций."""
