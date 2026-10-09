"""Надёжная REST-доставка без HTTP внутри SQL-транзакции."""

from memes_identity.application.dto.outbox import DeletionDestination
from memes_identity.application.ports.clock import IClock
from memes_identity.application.ports.outbox import IAccountDeletionPublisher
from memes_identity.application.ports.unit_of_work import IIdentityUnitOfWork


class DeliverAccountDeletion:
    """Аренда, внешний вызов и отдельное fenced подтверждение доставки."""

    def __init__(
        self,
        *,
        uow: IIdentityUnitOfWork,
        publisher: IAccountDeletionPublisher,
        clock: IClock,
    ) -> None:
        """Получить прикладные зависимости фонового процесса.

        Args:
            uow: Транзакция outbox.
            publisher: REST-адаптер независимого получателя.
            clock: Серверное время.
        """
        self._uow = uow
        self._publisher = publisher
        self._clock = clock

    async def execute(self, *, destination: DeletionDestination | None = None) -> bool:
        """Зафиксировать аренду до HTTP, затем отдельно подтвердить или отложить.

        Args:
            destination: Получатель administrative baseline либо все получатели.

        Returns:
            bool: Найдена ли доступная доставка.
        """
        async with self._uow:
            delivery = await self._uow.outbox.claim(self._clock.now(), destination=destination)
            if delivery is None:
                return False
            await self._uow.commit()
        accepted = await self._publisher.publish(delivery)
        async with self._uow:
            if accepted:
                await self._uow.outbox.acknowledge(delivery, self._clock.now())
            else:
                await self._uow.outbox.postpone(delivery, self._clock.now())
            await self._uow.commit()
        return True
