"""Серверная policy единой квоты приватных upload всех Search индексов."""

from datetime import datetime, timedelta
from enum import IntEnum
from uuid import UUID

from memes_kernel.errors import DomainError
from memes_kernel.values import require_aware, require_identifier
from memes_search.application.dto.sessions import SearchSessionPolicy
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.application.ports.clock import IClock
from memes_search.application.ports.sample_admission import ISampleAdmission
from memes_search.application.ports.sample_admission_ledger import ISampleAdmissionLedger


class AdmissionLimit(IntEnum):
    """Малый допуск часов между доверенными Search процессами."""

    CLOCK_SKEW_SECONDS = 30


class SharedSampleAdmission(ISampleAdmission):
    """Проверить server TTL перед атомарным резервом primary ledger."""

    def __init__(
        self, *, repository: ISampleAdmissionLedger, clock: IClock, policy: SearchSessionPolicy
    ) -> None:
        """Получить собственное время и серверные ограничения.

        Args:
            repository: Атомарный ledger primary Search.
            clock: Серверное время.
            policy: Предел квоты и срока из окружения получателя.
        """
        self._repository = repository
        self._clock = clock
        self._policy = policy

    async def reserve(
        self, *, sample_id: UUID, owner_id: UUID, created_at: datetime, expires_at: datetime
    ) -> None:
        """Отбить неверный TTL и передать получателю только server quota.

        Args:
            sample_id: Устойчивый ID передачи.
            owner_id: Проверенная идентичность.
            created_at: Время создания в доверенном sender.
            expires_at: Конечный срок, не превышающий server lifetime.

        Raises:
            SearchError: Неверные даты, исчерпанная квота либо owner fence.
        """
        try:
            require_identifier(sample_id)
            require_identifier(owner_id)
            require_aware(created_at)
            require_aware(expires_at)
        except DomainError as error:
            raise SearchError(SearchErrorCode.INVALID_SAMPLE) from error
        now = self._clock.now()
        if (
            expires_at <= now
            or expires_at <= created_at
            or expires_at - created_at > self._policy.lifetime
            or created_at > now + timedelta(seconds=AdmissionLimit.CLOCK_SKEW_SECONDS)
        ):
            raise SearchError(SearchErrorCode.INVALID_SAMPLE)
        await self._repository.reserve(
            sample_id=sample_id,
            owner_id=owner_id,
            created_at=created_at,
            expires_at=expires_at,
            now=now,
            limit=self._policy.active_samples_per_user,
        )

    async def release(self, sample_id: UUID) -> None:
        """Идемпотентно вернуть терминальный слот.

        Args:
            sample_id: ID образца.
        """
        await self._repository.release(sample_id)

    async def erase_owner(self, owner_id: UUID) -> None:
        """Установить fence в одной транзакции с очисткой reservations.

        Args:
            owner_id: Авторизованный удалённый владелец.
        """
        await self._repository.erase_owner(owner_id, self._clock.now())

    async def purge(self, now: datetime) -> None:
        """Передать primary maintenance конечную границу TTL.

        Args:
            now: Серверное время.
        """
        await self._repository.purge(now)
