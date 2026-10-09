"""Порт постраничного чтения обезличенных tombstone для нового Search."""

from abc import ABC, abstractmethod

from memes_identity.application.dto.deletion_baseline import DeletedOwner
from memes_identity.domain.user import UserId


class IDeletedOwnerSource(ABC):
    """Чтение tombstone без логинов, verifier и authentication sessions."""

    @abstractmethod
    async def page(self, *, after: UserId | None, limit: int) -> tuple[DeletedOwner, ...]:
        """Прочитать устойчивую страницу UUID удалённых владельцев.

        Args:
            after: Последний прочитанный UUID либо начало.
            limit: Максимальный размер страницы.
        """
