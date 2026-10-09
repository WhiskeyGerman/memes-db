"""Доверенный baseline удаления приватных данных перед открытием candidate Search."""

from enum import IntEnum, StrEnum
from uuid import UUID, uuid5

from memes_identity.application.dto.authentication import AccountDeleted
from memes_identity.application.dto.outbox import DeletionDestination, DeletionRecipient
from memes_identity.application.errors import IdentityError, IdentityFailure
from memes_identity.application.ports.clock import IClock
from memes_identity.application.ports.deletion_baseline import IDeletedOwnerSource
from memes_identity.application.ports.outbox import IAccountDeletionPublisher
from memes_identity.application.ports.unit_of_work import IIdentityUnitOfWork
from memes_identity.application.use_cases.outbox import DeliverAccountDeletion
from memes_identity.domain.user import UserId
from memes_kernel.values import Number


class BaselineLimit(IntEnum):
    """Ограничение памяти one-off процесса."""

    PAGE = 100


class BaselineIdentity(StrEnum):
    """Стабильное пространство ID идемпотентных baseline событий."""

    NAMESPACE = "c0b9bfcb-b230-4f16-9427-3497f19e2f43"


class SeedShadowSearchDeletions:
    """Установить durable owner fences только в configured candidate Search."""

    def __init__(
        self,
        *,
        uow: IIdentityUnitOfWork,
        source: IDeletedOwnerSource,
        publisher: IAccountDeletionPublisher,
        clock: IClock,
        destination: DeletionDestination,
    ) -> None:
        """Получить зависимости и доверенный candidate resource.

        Args:
            uow: Транзакции записи и подтверждения outbox.
            source: Обезличенные tombstone Identity.
            publisher: REST-адаптер независимого получателя.
            clock: Серверное время.
            destination: Только shadow Search текущего релиза.

        Raises:
            IdentityError: Выбран получатель, не являющийся shadow Search.
        """
        if destination.recipient is not DeletionRecipient.SHADOW_SEARCH:
            raise IdentityError(IdentityFailure.INVALID_REQUEST)
        self._uow = uow
        self._source = source
        self._publisher = publisher
        self._clock = clock
        self._destination = destination

    async def execute(self) -> int:
        """Засеять и доставить tombstone; при недоступности завершиться с отказом.

        Повтор безопасен: ID определяется владельцем и URL candidate. Обычный
        fan-out продолжает доставлять новые удаления во время чтения baseline.
        Ingress candidate открывается лишь после успешного завершения команды.

        Returns:
            int: Число прочитанных удалённых идентичностей.

        Raises:
            IdentityError: Остались retry или leases без durable acceptance.
        """
        after: UserId | None = None
        seeded: int = Number.ZERO
        while owners := await self._source.page(after=after, limit=BaselineLimit.PAGE):
            async with self._uow:
                for owner in owners:
                    event = AccountDeleted(
                        event_id=uuid5(
                            UUID(BaselineIdentity.NAMESPACE),
                            f"{self._destination.base_url.rstrip('/')}/{owner.user_id}",
                        ),
                        user_id=owner.user_id,
                        delete_media=False,
                        occurred_at=owner.deleted_at,
                    )
                    await self._uow.outbox.add(event, destination=self._destination)
                await self._uow.commit()
            seeded += len(owners)
            after = owners[-Number.ONE].user_id
        delivery = DeliverAccountDeletion(
            uow=self._uow, publisher=self._publisher, clock=self._clock
        )
        while await delivery.execute(destination=self._destination):
            pass
        async with self._uow:
            pending = await self._uow.outbox.pending(self._destination)
        if pending:
            raise IdentityError(IdentityFailure.BACKING_SERVICE_UNAVAILABLE)
        return int(seeded)
