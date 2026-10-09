"""Порты атомарных операций каталога и голосования."""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Self
from uuid import UUID

from memes_catalog.application.dto.catalog import (
    FeatureUpdate,
    FeedCursor,
    GroupView,
    MediaView,
    PendingCleanup,
    PublicationOutcome,
    SearchIndexTarget,
    Upload,
)
from memes_catalog.application.ports.variations import IVariationRepository
from memes_kernel.values import Sha256
from memes_platform.application.dto.json import JsonObject


class ICatalogUnitOfWork(ABC):
    """Общая транзакция публикации, членства, голосов и outbox."""

    @property
    @abstractmethod
    def variations(self) -> IVariationRepository:
        """Получить транзакционный порт вариаций."""

    @abstractmethod
    async def __aenter__(self) -> Self:
        """Открыть транзакцию."""

    @abstractmethod
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        """Откатить незафиксированное состояние.

        Args:
            exc_type: Тип ошибки.
            exc_value: Ошибка контекстного менеджера.
            traceback: Стек исключения.
        """

    @abstractmethod
    async def commit(self) -> None:
        """Зафиксировать изменения."""

    @abstractmethod
    async def reserve(
        self,
        upload: Upload,
        *,
        concurrent_limit: int,
        daily_limit: int,
        storage_limit: int,
        free_percent: int,
    ) -> None:
        """Атомарно зарезервировать квоты.

        Args:
            upload: Новая сессия передачи.
            concurrent_limit: Допустимые незавершённые передачи автора.
            daily_limit: Лимит публикаций за сутки.
            storage_limit: Выделенная ёмкость хранения.
            free_percent: Обязательная свободная доля.
        """

    @abstractmethod
    async def prepare_object(self, object_key: str, *, ready_at: datetime) -> None:
        """Зарегистрировать удаление неопубликованного объекта.

        Args:
            object_key: Ключ текущей попытки.
            ready_at: Срок гарантированной уборки.
        """

    @abstractmethod
    async def upload(self, upload_id: UUID, *, lock: bool = False) -> Upload | None:
        """Найти временную передачу.

        Args:
            upload_id: Идентификатор передачи.
            lock: Требуется ли блокировка.
        """

    @abstractmethod
    async def save_upload(self, upload: Upload) -> None:
        """Сохранить состояние передачи.

        Args:
            upload: Согласованное новое состояние.
        """

    @abstractmethod
    async def publish(
        self, upload: Upload, view: MediaView, *, now: datetime
    ) -> PublicationOutcome:
        """Сохранить уникальную публикацию вместе с singleton и outbox.

        Args:
            upload: Принятая передача.
            view: Согласованный новый снимок.
            now: Серверное время.
        """

    @abstractmethod
    async def get_media(self, media_id: UUID, *, lock: bool = False) -> MediaView | None:
        """Получить живую публикацию.

        Args:
            media_id: Идентификатор медиа.
            lock: Требуется ли блокировка.
        """

    @abstractmethod
    async def resolve_batch(self, media_ids: tuple[UUID, ...]) -> tuple[MediaView, ...]:
        """Получить публикации одной ограниченной пачкой.

        Args:
            media_ids: Проверяемые идентичности.
        """

    @abstractmethod
    async def by_hash(self, digest: Sha256) -> MediaView | None:
        """Найти активный оригинал.

        Args:
            digest: SHA-256.
        """

    @abstractmethod
    async def feed(self, *, before: FeedCursor | None, limit: int) -> tuple[MediaView, ...]:
        """Получить ограниченное окно ленты.

        Args:
            before: Курсор времени и идентичности.
            limit: Размер окна.
        """

    @abstractmethod
    async def group(self, group_id: UUID, *, offset: int, limit: int) -> GroupView | None:
        """Получить группу или актуального преемника.

        Args:
            group_id: Выбранная группа.
            offset: Смещение участников.
            limit: Размер окна.
        """

    @abstractmethod
    async def remove_media(self, view: MediaView, *, now: datetime) -> None:
        """Удалить только выбранную публикацию и закрыть членство.

        Args:
            view: Заблокированный снимок.
            now: Серверное время.
        """

    @abstractmethod
    async def account_deleted(
        self, *, event_id: UUID, user_id: UUID, delete_media: bool, now: datetime
    ) -> None:
        """Идемпотентно применить удаление аккаунта.

        Args:
            event_id: Идентичность сообщения.
            user_id: Обезличенный автор.
            delete_media: Выбор удаления его публикаций.
            now: Серверное время.
        """

    @abstractmethod
    async def dismissals(self, user_id: UUID, media_id: UUID) -> tuple[tuple[UUID, int], ...]:
        """Получить личные отклонения выбранного источника.

        Args:
            user_id: Участник.
            media_id: Источник предложения.
        """

    @abstractmethod
    async def deleted_accounts(self, *, limit: int) -> tuple[UUID, ...]:
        """Найти файлы, ожидающие выбранного удаления.

        Args:
            limit: Размер страницы.
        """

    @abstractmethod
    async def accept_feature(self, update: FeatureUpdate, *, now: datetime) -> bool:
        """Принять результат только живого источника и текущего поколения.

        Args:
            update: Проверенный результат.
            now: Серверное время.
        """

    @abstractmethod
    async def history(self, group_id: UUID, *, limit: int) -> tuple[JsonObject, ...]:
        """Получить безопасную историю решений.

        Args:
            group_id: Группа.
            limit: Размер окна.
        """

    @abstractmethod
    async def reindex(
        self,
        *,
        after: UUID | None,
        limit: int,
        now: datetime,
        target: SearchIndexTarget = SearchIndexTarget.PRIMARY,
    ) -> UUID | None:
        """Поставить ограниченную страницу проекций в durable outbox.

        Args:
            after: Последняя идентичность предыдущей страницы.
            limit: Размер пакета.
            now: Серверное время административной операции.
            target: Основной индекс или отдельно строящийся кандидат.
        """

    @abstractmethod
    async def cleanup(self, *, now: datetime, limit: int) -> tuple[PendingCleanup, ...]:
        """Выбрать просроченные и удаляемые объекты.

        Args:
            now: Серверное время.
            limit: Размер пакета.
        """

    @abstractmethod
    async def finish_cleanup(self, cleanup_id: UUID) -> None:
        """Зафиксировать удаление физического объекта.

        Args:
            cleanup_id: Задание удаления.
        """


class ICatalogUnitOfWorkFactory(ABC):
    """Фабрика транзакции текущего сценария."""

    @abstractmethod
    def open(self) -> ICatalogUnitOfWork:
        """Получить независимую транзакцию."""


class ICatalogHealth(ABC):
    """Порт проверки PostgreSQL для readiness."""

    @abstractmethod
    async def ready(self) -> bool:
        """Проверить ресурс без изменения схемы."""
