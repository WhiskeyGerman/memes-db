"""Порт хранения состояний голосования; решения принимает Application."""

from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from memes_catalog.domain.groups import GroupId, GroupMembership, MembershipId, VariationGroup
from memes_catalog.domain.voting import (
    BallotId,
    SuggestionDismissal,
    VariationBallot,
    VariationDecision,
    VariationVote,
)


class IVariationRepository(ABC):
    """Транзакционный порт загрузки и сохранения доменных состояний."""

    @abstractmethod
    async def lock_groups(self, group_ids: tuple[GroupId, ...]) -> tuple[VariationGroup, ...]:
        """Заблокировать группы в каноническом порядке.

        Args:
            group_ids: Идентификаторы групп.
        """

    @abstractmethod
    async def membership(
        self, membership_id: MembershipId, *, lock: bool = False
    ) -> GroupMembership | None:
        """Получить эпизод под блокировкой.

        Args:
            membership_id: Эпизод.
            lock: Требуется ли блокировка.
        """

    @abstractmethod
    async def ballot(self, ballot_id: BallotId) -> VariationBallot | None:
        """Получить голосование под блокировкой.

        Args:
            ballot_id: Голосование.
        """

    @abstractmethod
    async def open_ballot(self, key: str) -> VariationBallot | None:
        """Найти голосование по канонической актуальной цели.

        Args:
            key: Канонический ключ цели.
        """

    @abstractmethod
    async def add_ballot(self, ballot: VariationBallot, *, key: str) -> None:
        """Сохранить созданное доменом голосование.

        Args:
            ballot: Начальное состояние.
            key: Каноническая цель.
        """

    @abstractmethod
    async def vote(self, ballot_id: BallotId, voter_id: UUID) -> VariationVote | None:
        """Получить единственный голос аккаунта.

        Args:
            ballot_id: Голосование.
            voter_id: Аккаунт.
        """

    @abstractmethod
    async def save_ballot(self, ballot: VariationBallot) -> None:
        """Сохранить вычисленное доменом состояние.

        Args:
            ballot: Новое состояние.
        """

    @abstractmethod
    async def save_vote(self, vote: VariationVote) -> None:
        """Сохранить голос в общей транзакции.

        Args:
            vote: Согласованное состояние.
        """

    @abstractmethod
    async def save_group(self, group: VariationGroup) -> None:
        """Сохранить вычисленный состав.

        Args:
            group: Новое состояние.
        """

    @abstractmethod
    async def save_membership(self, membership: GroupMembership) -> None:
        """Сохранить вычисленный эпизод.

        Args:
            membership: Новое состояние.
        """

    @abstractmethod
    async def move_memberships(
        self, *, source_ids: tuple[GroupId, ...], target_id: GroupId, now: datetime
    ) -> None:
        """Массово завершить и пересоздать членство в выбранных группах.

        Args:
            source_ids: Группы, уже завершённые доменным решением.
            target_id: Новый согласованный состав.
            now: Время решения.
        """

    @abstractmethod
    async def obsolete(
        self,
        *,
        group_ids: tuple[GroupId, ...],
        membership_ids: tuple[MembershipId, ...],
        now: datetime,
    ) -> None:
        """Закрыть незавершённые ballot по потерявшим актуальность целям.

        Args:
            group_ids: Завершённые составы.
            membership_ids: Завершённые эпизоды.
            now: Время смены цели.
        """

    @abstractmethod
    async def save_decision(self, decision: VariationDecision) -> None:
        """Сохранить неизменяемый исторический факт.

        Args:
            decision: Исполненное решение.
        """

    @abstractmethod
    async def changed(self, group_ids: tuple[GroupId, ...], *, now: datetime) -> None:
        """Поставить обновление проекций в транзакционный outbox.

        Args:
            group_ids: Изменённые группы.
            now: Время события.
        """

    @abstractmethod
    async def limit_vote_rate(self, voter_id: UUID, *, maximum: int, now: datetime) -> None:
        """Атомарно проверить лимит частоты.

        Args:
            voter_id: Участник.
            maximum: Лимит за минуту.
            now: Серверное время.
        """

    @abstractmethod
    async def dismiss(self, dismissal: SuggestionDismissal) -> None:
        """Сохранить личное отклонение.

        Args:
            dismissal: Проверенная доменная запись.
        """
