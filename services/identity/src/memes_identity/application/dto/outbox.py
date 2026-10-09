"""Прикладные контракты независимой доставки уведомлений удаления."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from memes_identity.application.dto.authentication import AccountDeleted


class DeletionRecipient(StrEnum):
    """Независимые durable-получатели одного уведомления."""

    CATALOG = "catalog"
    SEARCH = "search"
    SHADOW_SEARCH = "shadow_search"


@dataclass(frozen=True, slots=True, kw_only=True)
class DeletionDestination:
    """Снимок подключаемого ресурса, сохранённый вместе с доставкой."""

    recipient: DeletionRecipient
    base_url: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountDeletionDelivery:
    """Одна независимо подтверждаемая доставка с fencing-токеном аренды."""

    delivery_id: UUID
    event: AccountDeleted
    destination: DeletionDestination
    claim_token: UUID
