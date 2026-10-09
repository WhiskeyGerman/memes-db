"""Ограниченные прикладные действия durable REST outbox и S3 очистки."""

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum

from memes_processing.application.dto.coordinator import JSONValue, Payload
from memes_processing.application.ports.external import (
    DeliveryUnavailableError,
    IArtifactStorage,
    IEventDelivery,
)
from memes_processing.application.ports.repository import IProcessingAction, IProcessingRepository


class DeliveryLimit(IntEnum):
    """Одна внешняя операция удерживает одну короткую транзакцию."""

    TRANSACTION_BATCH = 1


@dataclass(frozen=True, slots=True, kw_only=True)
class CleanArtifact(IProcessingAction):
    """Повторная очистка безопасна после потери подтверждения S3."""

    storage: IArtifactStorage
    now: datetime

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Подтвердить удаление только после успешного ответа хранилища."""
        keys = await repository.cleanup(now=self.now, limit=DeliveryLimit.TRANSACTION_BATCH)
        for key in keys:
            await self.storage.delete(key)
            await repository.cleanup_done(key)
        return {"processed": len(keys)}


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliverOutbox(IProcessingAction):
    """Выбор очереди и retry принадлежат прикладному сценарию."""

    delivery: IEventDelivery
    now: datetime

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """ACK и задержка надежно сохраняются в той же транзакции."""
        events = await repository.outgoing(now=self.now, limit=DeliveryLimit.TRANSACTION_BATCH)
        retried: list[JSONValue] = []
        for event in events:
            try:
                await self.delivery.deliver(event)
            except DeliveryUnavailableError:
                await repository.postpone(event, now=self.now)
                retried.append(str(event.id))
            else:
                await repository.acknowledge(event.id)
        return {"processed": len(events), "retried_event_ids": retried}
