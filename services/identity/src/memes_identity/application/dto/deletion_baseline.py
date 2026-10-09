"""Обезличенная прикладная проекция удалённого владельца."""

from dataclasses import dataclass
from datetime import datetime

from memes_identity.domain.user import UserId


@dataclass(frozen=True, slots=True, kw_only=True)
class DeletedOwner:
    """Техническая идентичность окончательно удалённого владельца."""

    user_id: UserId
    deleted_at: datetime
