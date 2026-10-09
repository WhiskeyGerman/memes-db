"""Порты независимой durable REST-доставки удаления аккаунта."""

from abc import ABC, abstractmethod
from datetime import datetime

from memes_identity.application.dto.authentication import AccountDeleted
from memes_identity.application.dto.outbox import AccountDeletionDelivery, DeletionDestination


class IAccountOutbox(ABC):
    """Уведомления и независимо подтверждаемые получатели в транзакции удаления."""

    @abstractmethod
    async def add(
        self, event: AccountDeleted, *, destination: DeletionDestination | None = None
    ) -> None:
        """Сохранить уведомление и получателей; :data:`None` выбирает конфигурацию релиза.

        Args:
            event: Уведомление удаления без персональных данных.
            destination: Единственный получатель административного baseline.
        """

    @abstractmethod
    async def claim(
        self, now: datetime, *, destination: DeletionDestination | None = None
    ) -> AccountDeletionDelivery | None:
        """Арендовать доступную доставку до commit без удержания HTTP под SQL lock.

        Args:
            now: Серверное время.
            destination: Ограничение administrative drain одним ресурсом.
        """

    @abstractmethod
    async def acknowledge(self, delivery: AccountDeletionDelivery, now: datetime) -> None:
        """Подтвердить доставку только при действующей собственной аренде.

        Args:
            delivery: Доставка с fencing-токеном.
            now: Время подтверждения.
        """

    @abstractmethod
    async def postpone(self, delivery: AccountDeletionDelivery, now: datetime) -> None:
        """Освободить собственную аренду с ограниченным backoff.

        Args:
            delivery: Неудавшаяся доставка.
            now: Серверное время.
        """

    @abstractmethod
    async def pending(self, destination: DeletionDestination) -> int:
        """Посчитать неподтверждённые доставки в выбранный ресурс.

        Args:
            destination: Получатель baseline, включая отложенные доставки.
        """


class IAccountDeletionPublisher(ABC):
    """REST-адаптер одной независимо подтверждаемой доставки."""

    @abstractmethod
    async def publish(self, delivery: AccountDeletionDelivery) -> bool:
        """Дождаться durable acceptance одного получателя.

        Args:
            delivery: Уведомление и сохранённый адрес получателя.
        """
