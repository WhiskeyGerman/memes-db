"""Формирование транспортных проекций каталога для REST outbox."""

from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from memes_catalog.infrastructure.persistence.models import OutboxRow
from memes_platform.application.dto.json import JsonObject


class DeliveryTarget(StrEnum):
    """Адресуемые владельцы проекций."""

    PUBLISHED = "processing:/internal/media/published"
    DELETED = "processing:/internal/media/deleted"
    SEARCH_STATE = "search:/internal/search/media-state"
    SEARCH_FEATURE = "search:/internal/search/features"
    SHADOW_STATE = "search-shadow:/internal/search/media-state"
    SHADOW_FEATURE = "search-shadow:/internal/search/features"


class OutboxSetting(StrEnum):
    """Инфраструктурные параметры транзакционного транспорта."""

    MIRROR_SEARCH = "mirror_search"


def delivery_targets(session: AsyncSession, target: DeliveryTarget) -> tuple[DeliveryTarget, ...]:
    """Выбрать независимые очереди обоих индексов при активной миграции.

    Args:
        session: Текущая SQL-транзакция с конфигурацией адаптера.
        target: Основной логический получатель.
    """
    if session.info.get(OutboxSetting.MIRROR_SEARCH):
        mirrors = {
            DeliveryTarget.SEARCH_STATE: DeliveryTarget.SHADOW_STATE,
            DeliveryTarget.SEARCH_FEATURE: DeliveryTarget.SHADOW_FEATURE,
        }
        shadow = mirrors.get(target)
        if shadow is not None:
            return target, shadow
    return (target,)


def enqueue(
    session: AsyncSession, target: DeliveryTarget, payload: JsonObject, now: datetime
) -> None:
    """Сохранить независимые события в общей транзакции.

    Args:
        session: Транзакция.
        target: Получатель.
        payload: Полезная нагрузка.
        now: Серверное время.
    """
    for destination in delivery_targets(session, target):
        event_id = uuid4()
        session.add(
            OutboxRow(
                id=event_id,
                target=destination.value,
                payload={**payload, "event_id": str(event_id)},
                created_at=now,
                ready_at=now,
                attempts=0,
                claim_id=None,
                claimed_until=None,
                delivered_at=None,
            )
        )
