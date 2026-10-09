"""Голосование за изменение принадлежности медиа группам."""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import NewType, TypeAlias
from uuid import UUID

from memes_catalog.domain.groups import DetachTarget, GroupId, GroupRef, MergeTarget
from memes_catalog.domain.media import AuthorId, MediaId
from memes_kernel.entity import Entity
from memes_kernel.errors import DomainError
from memes_kernel.values import Number, require_identifier, require_ordered

BallotId = NewType("BallotId", UUID)
VoteId = NewType("VoteId", UUID)
DecisionId = NewType("DecisionId", UUID)
DismissalId = NewType("DismissalId", UUID)
VariationTarget: TypeAlias = MergeTarget | DetachTarget


class VoteThreshold(IntEnum):
    """Начальные пороги первой версии."""

    MERGE = 1
    DETACH = 10


class BallotState(StrEnum):
    """Состояние решения пользователей."""

    OPEN = "open"
    REACHED = "reached"
    APPLIED = "applied"
    OBSOLETE = "obsolete"


class VotingViolation(StrEnum):
    """Нарушения правил голосования."""

    INVALID_THRESHOLD = "invalid_threshold"
    INVALID_BALLOT = "invalid_ballot"
    CLOSED_BALLOT = "closed_ballot"
    FOREIGN_VOTE = "foreign_vote"
    INVALID_VOTE = "invalid_vote"
    THRESHOLD_NOT_REACHED = "threshold_not_reached"
    INVALID_DECISION = "invalid_decision"


@dataclass(frozen=True, slots=True, kw_only=True)
class VotingRule:
    """Неизменяемый снимок порога для конкретного голосования."""

    threshold: int
    policy_revision: UUID

    def __post_init__(self) -> None:
        """Проверить положительный порог и ревизию политики."""
        require_identifier(self.policy_revision)
        if self.threshold < Number.ONE:
            raise DomainError(VotingViolation.INVALID_THRESHOLD)


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class VariationVote(Entity[VoteId]):
    """Один голос учётной записи с возможностью отзыва."""

    ballot_id: BallotId
    voter_id: AuthorId
    cast_at: datetime
    withdrawn_at: datetime | None = None

    def __post_init__(self) -> None:
        """Проверить идентификаторы и порядок времени."""
        Entity.__post_init__(self)
        require_identifier(self.ballot_id)
        require_identifier(self.voter_id)
        require_ordered(self.cast_at, self.cast_at)
        if self.withdrawn_at is not None:
            require_ordered(self.withdrawn_at, self.cast_at)

    @property
    def active(self) -> bool:
        """Проверить, участвует ли голос в счётчике."""
        return self.withdrawn_at is None

    def withdraw(self, *, now: datetime) -> "VariationVote":
        """Отозвать активный голос.

        Args:
            now: Серверное время отзыва.
        """
        if not self.active:
            return self
        require_ordered(now, self.cast_at)
        return replace(self, withdrawn_at=now, revision=self.revision.next())

    def recast(self, *, now: datetime) -> "VariationVote":
        """Подать заново ранее отозванный голос.

        Args:
            now: Серверное время повторного решения.
        """
        if self.active:
            return self
        require_ordered(now, self.withdrawn_at or self.cast_at)
        return replace(self, cast_at=now, withdrawn_at=None, revision=self.revision.next())


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class VariationBallot(Entity[BallotId]):
    """Голосование со счётчиком и зафиксированной целью.

    Уникальные голоса хранятся отдельно; изменение голоса и счётчика
    должно происходить в одной транзакции.
    """

    target: VariationTarget
    rule: VotingRule
    opened_at: datetime
    active_votes: int = Number.ZERO
    state: BallotState = BallotState.OPEN
    closed_at: datetime | None = None
    decision_id: DecisionId | None = None

    def __post_init__(self) -> None:
        """Проверить согласованность счётчика и результата."""
        Entity.__post_init__(self)
        require_ordered(self.opened_at, self.opened_at)
        if self.active_votes < Number.ZERO:
            raise DomainError(VotingViolation.INVALID_BALLOT)
        reached = self.active_votes >= self.rule.threshold
        if self.state is BallotState.OPEN and reached:
            raise DomainError(VotingViolation.INVALID_BALLOT)
        if self.state in {BallotState.REACHED, BallotState.APPLIED} and not reached:
            raise DomainError(VotingViolation.INVALID_BALLOT)
        closed = self.state in {BallotState.APPLIED, BallotState.OBSOLETE}
        if closed != (self.closed_at is not None):
            raise DomainError(VotingViolation.INVALID_BALLOT)
        if (self.state is BallotState.APPLIED) != (self.decision_id is not None):
            raise DomainError(VotingViolation.INVALID_BALLOT)
        if self.closed_at is not None:
            require_ordered(self.closed_at, self.opened_at)
        if self.decision_id is not None:
            require_identifier(self.decision_id)

    def cast(
        self,
        *,
        vote_id: VoteId,
        voter_id: AuthorId,
        existing: VariationVote | None,
        now: datetime,
    ) -> tuple["VariationBallot", VariationVote]:
        """Подать уникальный голос в текущем голосовании.

        Args:
            vote_id: Идентификатор нового голоса.
            voter_id: Зарегистрированный участник.
            existing: Голос той же учётной записи, загруженный под блокировкой.
            now: Серверное время.

        Returns:
            tuple: Новое состояние голосования и голоса.
        """
        self._check_vote(existing, voter_id)
        if existing is not None and existing.active:
            return self, existing
        self._require_open()
        require_ordered(now, self.opened_at)
        vote = (
            existing.recast(now=now)
            if existing is not None
            else VariationVote(id=vote_id, ballot_id=self.id, voter_id=voter_id, cast_at=now)
        )
        count = self.active_votes + Number.ONE
        state = BallotState.REACHED if count >= self.rule.threshold else BallotState.OPEN
        return replace(self, active_votes=count, state=state, revision=self.revision.next()), vote

    def withdraw(
        self,
        *,
        vote: VariationVote,
        now: datetime,
    ) -> tuple["VariationBallot", VariationVote]:
        """Отозвать голос до исполнения изменения.

        Args:
            vote: Текущий голос участника.
            now: Серверное время.

        Returns:
            tuple: Новое состояние голосования и голоса.
        """
        self._check_vote(vote, vote.voter_id)
        self._require_open()
        if not vote.active:
            return self, vote
        if self.active_votes < Number.ONE:
            raise DomainError(VotingViolation.INVALID_BALLOT)
        return replace(
            self, active_votes=self.active_votes - Number.ONE, revision=self.revision.next()
        ), vote.withdraw(now=now)

    def apply(self, *, decision_id: DecisionId, now: datetime) -> "VariationBallot":
        """Зафиксировать исполнение после достижения порога.

        Args:
            decision_id: Идентификатор исполненного решения.
            now: Серверное время.

        Raises:
            DomainError: Порог не достигнут или решение уже изменилось.
        """
        if self.state is BallotState.APPLIED and self.decision_id == decision_id:
            return self
        if self.state is not BallotState.REACHED:
            raise DomainError(VotingViolation.THRESHOLD_NOT_REACHED)
        require_ordered(now, self.opened_at)
        return replace(
            self,
            state=BallotState.APPLIED,
            decision_id=decision_id,
            closed_at=now,
            revision=self.revision.next(),
        )

    def invalidate(self, *, now: datetime) -> "VariationBallot":
        """Закрыть неисполненное голосование при смене его цели.

        Args:
            now: Серверное время изменения состава.
        """
        if self.state in {BallotState.APPLIED, BallotState.OBSOLETE}:
            return self
        require_ordered(now, self.opened_at)
        return replace(
            self, state=BallotState.OBSOLETE, closed_at=now, revision=self.revision.next()
        )

    def _require_open(self) -> None:
        """Потребовать возможность изменить голос."""
        if self.state is not BallotState.OPEN:
            raise DomainError(VotingViolation.CLOSED_BALLOT)

    def _check_vote(self, vote: VariationVote | None, voter_id: AuthorId) -> None:
        """Проверить принадлежность загруженного голоса."""
        if vote is not None and (vote.ballot_id != self.id or vote.voter_id != voter_id):
            raise DomainError(VotingViolation.FOREIGN_VOTE)


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class VariationDecision(Entity[DecisionId]):
    """Исторический факт исполненного изменения."""

    ballot_id: BallotId
    target: VariationTarget
    rule: VotingRule
    counted_votes: int
    resulting_group_id: GroupId
    applied_at: datetime

    def __post_init__(self) -> None:
        """Проверить достаточное основание решения."""
        Entity.__post_init__(self)
        require_identifier(self.ballot_id)
        require_identifier(self.resulting_group_id)
        require_ordered(self.applied_at, self.applied_at)
        if self.counted_votes < self.rule.threshold:
            raise DomainError(VotingViolation.INVALID_DECISION)


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class SuggestionDismissal(Entity[DismissalId]):
    """Личное отклонение предложения по определённому составу группы."""

    user_id: AuthorId
    source_media_id: MediaId
    candidate_group: GroupRef
    dismissed_at: datetime

    def __post_init__(self) -> None:
        """Проверить идентификаторы и время решения."""
        Entity.__post_init__(self)
        require_identifier(self.user_id)
        require_identifier(self.source_media_id)
        require_ordered(self.dismissed_at, self.dismissed_at)
