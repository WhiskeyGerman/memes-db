"""PostgreSQL outbox: независимые получатели, короткие SQL leases и fencing."""

from datetime import datetime, timedelta
from enum import IntEnum
from uuid import uuid4

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from memes_identity.application.dto.authentication import AccountDeleted
from memes_identity.application.dto.outbox import (
    AccountDeletionDelivery,
    DeletionDestination,
    DeletionRecipient,
)
from memes_identity.application.ports.outbox import IAccountOutbox
from memes_identity.domain.user import UserId
from memes_identity.infrastructure.configuration import IdentitySettings
from memes_identity.infrastructure.persistence.models import AccountDeliveryRow, AccountOutboxRow
from memes_kernel.values import Number


class OutboxBackoff(IntEnum):
    """Ограниченная экспонента повторной доставки."""

    FACTOR = 2
    MAX_EXPONENT = 16


class SqlAccountOutbox(IAccountOutbox):
    """Доставки сохраняют URL релиза и подтверждаются независимо друг от друга."""

    def __init__(self, session: AsyncSession, settings: IdentitySettings) -> None:
        """Получить транзакцию, снимок подключаемых ресурсов и retry policy.

        Args:
            session: PostgreSQL-сессия.
            settings: Конфигурация доставки.
        """
        self._db = session
        self._settings = settings

    async def add(
        self, event: AccountDeleted, *, destination: DeletionDestination | None = None
    ) -> None:
        """Сохранить событие и независимые назначения в транзакции удаления.

        Args:
            event: Уведомление удаления.
            destination: Единственный получатель baseline либо configured fan-out.
        """
        await self._db.execute(
            insert(AccountOutboxRow)
            .values(
                event_id=event.event_id,
                user_id=event.user_id,
                delete_media=event.delete_media,
                occurred_at=event.occurred_at,
                next_attempt_at=event.occurred_at,
                attempts=Number.ZERO,
                delivered_at=None,
            )
            .on_conflict_do_nothing(index_elements=[AccountOutboxRow.event_id])
        )
        destinations = (destination,) if destination is not None else self._destinations()
        added = False
        for target in destinations:
            delivery_id = await self._db.scalar(
                insert(AccountDeliveryRow)
                .values(
                    id=uuid4(),
                    event_id=event.event_id,
                    recipient=target.recipient,
                    base_url=target.base_url.rstrip("/"),
                    next_attempt_at=event.occurred_at,
                    attempts=Number.ZERO,
                    delivered_at=None,
                    claim_token=None,
                    lease_until=None,
                )
                .on_conflict_do_nothing(constraint="uq_identity_delivery_target")
                .returning(AccountDeliveryRow.id)
            )
            added = added or delivery_id is not None
        if added:
            await self._db.execute(
                update(AccountOutboxRow)
                .where(AccountOutboxRow.event_id == event.event_id)
                .values(delivered_at=None)
            )

    async def claim(
        self, now: datetime, *, destination: DeletionDestination | None = None
    ) -> AccountDeletionDelivery | None:
        """Зафиксировать короткую аренду; SQL lock заканчивается перед HTTP.

        Args:
            now: Серверное время.
            destination: Ограничение одним административным получателем.
        """
        await self._expand_legacy()
        statement = (
            select(AccountDeliveryRow, AccountOutboxRow)
            .join(AccountOutboxRow, AccountOutboxRow.event_id == AccountDeliveryRow.event_id)
            .where(
                AccountDeliveryRow.delivered_at.is_(None),
                AccountDeliveryRow.next_attempt_at <= now,
                or_(
                    AccountDeliveryRow.lease_until.is_(None), AccountDeliveryRow.lease_until <= now
                ),
            )
            .order_by(AccountDeliveryRow.next_attempt_at, AccountDeliveryRow.id)
            .limit(Number.ONE)
            .with_for_update(skip_locked=True, of=AccountDeliveryRow)
        )
        if destination is not None:
            statement = statement.where(
                AccountDeliveryRow.recipient == destination.recipient,
                AccountDeliveryRow.base_url == destination.base_url.rstrip("/"),
            )
        found = (await self._db.execute(statement)).first()
        if found is None:
            return None
        row, event = found
        token = uuid4()
        row.claim_token = token
        row.lease_until = now + timedelta(seconds=self._settings.outbox_lease_seconds)
        await self._db.flush()
        return AccountDeletionDelivery(
            delivery_id=row.id,
            event=AccountDeleted(
                event_id=event.event_id,
                user_id=UserId(event.user_id),
                delete_media=event.delete_media,
                occurred_at=event.occurred_at,
            ),
            destination=DeletionDestination(
                recipient=DeletionRecipient(row.recipient), base_url=row.base_url
            ),
            claim_token=token,
        )

    async def acknowledge(self, delivery: AccountDeletionDelivery, now: datetime) -> None:
        """Подтвердить только действующую аренду; завершить событие после всех получателей.

        Args:
            delivery: Подтверждаемая доставка.
            now: Серверное время.
        """
        event_id = await self._db.scalar(
            update(AccountDeliveryRow)
            .where(
                AccountDeliveryRow.id == delivery.delivery_id,
                AccountDeliveryRow.claim_token == delivery.claim_token,
                AccountDeliveryRow.lease_until > now,
                AccountDeliveryRow.delivered_at.is_(None),
            )
            .values(delivered_at=now, claim_token=None, lease_until=None)
            .returning(AccountDeliveryRow.event_id)
        )
        if event_id is None:
            return
        # Общая строка сериализует последнее подтверждение конкурентных получателей.
        await self._db.scalar(
            select(AccountOutboxRow.event_id)
            .where(AccountOutboxRow.event_id == event_id)
            .with_for_update()
        )
        outstanding = exists().where(
            AccountDeliveryRow.event_id == event_id, AccountDeliveryRow.delivered_at.is_(None)
        )
        await self._db.execute(
            update(AccountOutboxRow)
            .where(AccountOutboxRow.event_id == event_id, ~outstanding)
            .values(delivered_at=now)
        )

    async def postpone(self, delivery: AccountDeletionDelivery, now: datetime) -> None:
        """Отложить только свою действующую аренду, сохранив исходное уведомление.

        Args:
            delivery: Неудавшаяся доставка.
            now: Серверное время.
        """
        row = await self._db.scalar(
            select(AccountDeliveryRow)
            .where(
                AccountDeliveryRow.id == delivery.delivery_id,
                AccountDeliveryRow.claim_token == delivery.claim_token,
                AccountDeliveryRow.lease_until > now,
                AccountDeliveryRow.delivered_at.is_(None),
            )
            .with_for_update()
        )
        if row is None:
            return
        delay = min(
            self._settings.outbox_retry_max_seconds,
            self._settings.outbox_retry_seconds
            * OutboxBackoff.FACTOR ** min(row.attempts, OutboxBackoff.MAX_EXPONENT),
        )
        row.attempts += Number.ONE
        row.next_attempt_at = now + timedelta(seconds=delay)
        row.claim_token = None
        row.lease_until = None

    async def pending(self, destination: DeletionDestination) -> int:
        """Посчитать все неподтверждённые назначения, включая retry и active leases.

        Args:
            destination: Конкретный ресурс baseline.
        """
        count = await self._db.scalar(
            select(func.count())
            .select_from(AccountDeliveryRow)
            .where(
                AccountDeliveryRow.recipient == destination.recipient,
                AccountDeliveryRow.base_url == destination.base_url.rstrip("/"),
                AccountDeliveryRow.delivered_at.is_(None),
            )
        )
        return int(count or Number.ZERO)

    def _destinations(self) -> tuple[DeletionDestination, ...]:
        """Получить configured fan-out для нового события либо старого undelivered outbox."""
        self._settings.require_outbox_configuration()
        targets: tuple[DeletionDestination, ...] = (
            DeletionDestination(
                recipient=DeletionRecipient.CATALOG, base_url=self._settings.catalog_url
            ),
            DeletionDestination(
                recipient=DeletionRecipient.SEARCH, base_url=self._settings.search_url
            ),
        )
        if self._settings.shadow_search_url:
            targets += (
                DeletionDestination(
                    recipient=DeletionRecipient.SHADOW_SEARCH,
                    base_url=self._settings.shadow_search_url,
                ),
            )
        return targets

    async def _expand_legacy(self) -> None:
        """Постранично продолжить undelivered события, созданные до миграции получателей."""
        row = await self._db.scalar(
            select(AccountOutboxRow)
            .where(
                AccountOutboxRow.delivered_at.is_(None),
                ~exists().where(AccountDeliveryRow.event_id == AccountOutboxRow.event_id),
            )
            .order_by(AccountOutboxRow.occurred_at, AccountOutboxRow.event_id)
            .limit(Number.ONE)
            .with_for_update(skip_locked=True)
        )
        if row is not None:
            await self.add(
                AccountDeleted(
                    event_id=row.event_id,
                    user_id=UserId(row.user_id),
                    delete_media=row.delete_media,
                    occurred_at=row.occurred_at,
                )
            )
