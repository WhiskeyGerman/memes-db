"""Доставка durable REST outbox с явным повтором известных сбоев."""

import asyncio
from datetime import datetime

from memes_catalog.application.dto.catalog import CatalogDefault, OutgoingEvent
from memes_catalog.application.ports.outbox import IOutboxRepository
from memes_catalog.application.ports.resources import IRestDelivery
from memes_platform.application.errors import ApplicationError


class DeliverEvents:
    """Прикладной процесс доставки, независимый от ORM и HTTP SDK."""

    def __init__(self, repository: IOutboxRepository, delivery: IRestDelivery) -> None:
        """Сохранить порты.

        Args:
            repository: Очередь.
            delivery: Получатель.
        """
        self.repository = repository
        self.delivery = delivery

    async def execute(self, *, now: datetime, limit: int) -> int:
        """Доставить ограниченную порцию событий.

        Args:
            now: Серверное время.
            limit: Размер порции.
        """
        events = await self.repository.acquire(
            now=now, limit=min(limit, CatalogDefault.DELIVERY_CONCURRENCY)
        )
        await asyncio.gather(*(self._deliver(event, now=now) for event in events))
        return len(events)

    async def _deliver(self, event: OutgoingEvent, *, now: datetime) -> None:
        """Подтвердить отдельный lease после успешного REST-ответа."""
        try:
            await self.delivery.deliver(event.target, event.payload)
        except ApplicationError:
            await self.repository.retry(event, now=now)
        else:
            await self.repository.acknowledge(event.id, event.claim_id)
