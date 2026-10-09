"""Чистые операции исполнения решений пользователей."""

from dataclasses import dataclass
from datetime import datetime

from memes_catalog.domain.groups import (
    DetachTarget,
    GroupId,
    GroupMembership,
    GroupViolation,
    MembershipEnd,
    MembershipId,
    MergeTarget,
    VariationGroup,
)
from memes_catalog.domain.voting import (
    BallotState,
    DecisionId,
    VariationBallot,
    VariationDecision,
    VotingViolation,
)
from memes_kernel.errors import DomainError
from memes_kernel.values import Number, require_ordered


@dataclass(frozen=True, slots=True, kw_only=True)
class MergeChange:
    """Согласованный результат объединения без загрузки всех участников."""

    first: VariationGroup
    second: VariationGroup
    successor: VariationGroup
    ballot: VariationBallot
    decision: VariationDecision


@dataclass(frozen=True, slots=True, kw_only=True)
class DetachChange:
    """Согласованный результат открепления одного медиа."""

    remaining: VariationGroup
    singleton: VariationGroup
    closed_membership: GroupMembership
    membership: GroupMembership
    ballot: VariationBallot
    decision: VariationDecision


class VariationGovernance:
    """Правила исполнения групповых решений внутри транзакции каталога."""

    @staticmethod
    def merge(
        *,
        first: VariationGroup,
        second: VariationGroup,
        ballot: VariationBallot,
        successor_id: GroupId,
        decision_id: DecisionId,
        now: datetime,
    ) -> MergeChange:
        """Объединить актуальные составы после достижения порога.

        Args:
            first: Первая заблокированная группа.
            second: Вторая заблокированная группа.
            ballot: Голосование.
            successor_id: Новый идентификатор группы.
            decision_id: Новый идентификатор решения.
            now: Серверное время.

        Returns:
            MergeChange: Согласованные состояния для атомарного сохранения.
        """
        if ballot.state is not BallotState.REACHED or not isinstance(ballot.target, MergeTarget):
            raise DomainError(VotingViolation.THRESHOLD_NOT_REACHED)
        if {first.id, second.id} != {ballot.target.first.id, ballot.target.second.id}:
            raise DomainError(GroupViolation.INVALID_TARGET)
        groups = {first.id: first, second.id: second}
        groups[ballot.target.first.id].require_reference(ballot.target.first)
        groups[ballot.target.second.id].require_reference(ballot.target.second)
        if successor_id in groups:
            raise DomainError(GroupViolation.SAME_GROUP)
        require_ordered(now, first.created_at)
        require_ordered(now, second.created_at)
        successor = VariationGroup(
            id=successor_id, member_count=first.member_count + second.member_count, created_at=now
        )
        decision = VariationDecision(
            id=decision_id,
            ballot_id=ballot.id,
            target=ballot.target,
            rule=ballot.rule,
            counted_votes=ballot.active_votes,
            resulting_group_id=successor_id,
            applied_at=now,
        )
        return MergeChange(
            first=first.retire_into(successor_id),
            second=second.retire_into(successor_id),
            successor=successor,
            ballot=ballot.apply(decision_id=decision_id, now=now),
            decision=decision,
        )

    @staticmethod
    def detach(
        *,
        group: VariationGroup,
        membership: GroupMembership,
        ballot: VariationBallot,
        singleton_id: GroupId,
        membership_id: MembershipId,
        decision_id: DecisionId,
        now: datetime,
    ) -> DetachChange:
        """Открепить актуальный эпизод членства.

        Args:
            group: Текущая заблокированная группа.
            membership: Текущий эпизод членства медиа.
            ballot: Голосование.
            singleton_id: Новая одиночная группа.
            membership_id: Новый эпизод членства.
            decision_id: Новый идентификатор решения.
            now: Серверное время.

        Returns:
            DetachChange: Согласованные состояния для атомарного сохранения.
        """
        if ballot.state is not BallotState.REACHED or not isinstance(ballot.target, DetachTarget):
            raise DomainError(VotingViolation.THRESHOLD_NOT_REACHED)
        group.require_active()
        if (
            ballot.target.membership_id != membership.id
            or membership.group_id != group.id
            or not membership.active
            or group.member_count <= Number.ONE
        ):
            raise DomainError(GroupViolation.INVALID_MEMBERSHIP)
        if singleton_id == group.id or membership_id == membership.id:
            raise DomainError(GroupViolation.SAME_GROUP)
        require_ordered(now, group.created_at)
        singleton = VariationGroup(id=singleton_id, created_at=now)
        new_membership = GroupMembership(
            id=membership_id,
            media_id=membership.media_id,
            group_id=singleton_id,
            joined_at=now,
        )
        decision = VariationDecision(
            id=decision_id,
            ballot_id=ballot.id,
            target=ballot.target,
            rule=ballot.rule,
            counted_votes=ballot.active_votes,
            resulting_group_id=singleton_id,
            applied_at=now,
        )
        return DetachChange(
            remaining=group.remove_member(),
            singleton=singleton,
            closed_membership=membership.close(reason=MembershipEnd.DETACHED, now=now),
            membership=new_membership,
            ballot=ballot.apply(decision_id=decision_id, now=now),
            decision=decision,
        )
