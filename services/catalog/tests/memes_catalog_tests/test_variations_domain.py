from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from uuid import uuid4

import pytest

from memes_catalog.domain.governance import VariationGovernance
from memes_catalog.domain.groups import (
    DetachTarget,
    GroupId,
    GroupMembership,
    GroupState,
    GroupViolation,
    MembershipId,
    MergeTarget,
    VariationGroup,
)
from memes_catalog.domain.media import AuthorId
from memes_catalog.domain.voting import (
    BallotState,
    DecisionId,
    VariationBallot,
    VariationTarget,
    VoteId,
    VoteThreshold,
    VotingViolation,
)
from memes_kernel.errors import DomainError
from memes_kernel.values import Number


def test_merge_target_is_canonical(group: VariationGroup, other_group: VariationGroup) -> None:
    assert MergeTarget.between(group.reference, other_group.reference) == MergeTarget.between(
        other_group.reference, group.reference
    )


def test_duplicate_vote_does_not_advance_threshold(
    membership: GroupMembership,
    make_ballot: Callable[[VariationTarget, int], VariationBallot],
    now: datetime,
) -> None:
    ballot = make_ballot(DetachTarget(membership_id=membership.id), VoteThreshold.DETACH)
    voter = AuthorId(uuid4())
    first, vote = ballot.cast(vote_id=VoteId(uuid4()), voter_id=voter, existing=None, now=now)
    repeated, repeated_vote = first.cast(
        vote_id=VoteId(uuid4()), voter_id=voter, existing=vote, now=now
    )
    assert repeated is first
    assert repeated_vote is vote
    assert repeated.active_votes == Number.ONE


def test_withdraw_and_recast_preserve_single_vote_identity(
    membership: GroupMembership,
    make_ballot: Callable[[VariationTarget, int], VariationBallot],
    now: datetime,
) -> None:
    ballot = make_ballot(DetachTarget(membership_id=membership.id), VoteThreshold.DETACH)
    voter = AuthorId(uuid4())
    cast, vote = ballot.cast(vote_id=VoteId(uuid4()), voter_id=voter, existing=None, now=now)
    withdrawn, old_vote = cast.withdraw(vote=vote, now=now)
    assert withdrawn.active_votes == Number.ZERO
    restored, new_vote = withdrawn.cast(
        vote_id=VoteId(uuid4()), voter_id=voter, existing=old_vote, now=now
    )
    assert restored.active_votes == Number.ONE
    assert new_vote.id == vote.id


def test_foreign_vote_is_rejected(
    membership: GroupMembership,
    make_ballot: Callable[[VariationTarget, int], VariationBallot],
    now: datetime,
) -> None:
    ballot = make_ballot(DetachTarget(membership_id=membership.id), VoteThreshold.DETACH)
    voter = AuthorId(uuid4())
    _, vote = ballot.cast(vote_id=VoteId(uuid4()), voter_id=voter, existing=None, now=now)
    with pytest.raises(DomainError) as error:
        ballot.cast(vote_id=VoteId(uuid4()), voter_id=AuthorId(uuid4()), existing=vote, now=now)
    assert error.value.code is VotingViolation.FOREIGN_VOTE


def test_merge_retires_both_groups_and_creates_fresh_group(
    group: VariationGroup,
    other_group: VariationGroup,
    make_ballot: Callable[[VariationTarget, int], VariationBallot],
    reach_threshold: Callable[[VariationBallot], VariationBallot],
    now: datetime,
) -> None:
    ballot = reach_threshold(
        make_ballot(
            MergeTarget.between(group.reference, other_group.reference), VoteThreshold.MERGE
        )
    )
    change = VariationGovernance.merge(
        first=group,
        second=other_group,
        ballot=ballot,
        successor_id=GroupId(uuid4()),
        decision_id=DecisionId(uuid4()),
        now=now,
    )
    assert change.first.state is GroupState.MERGED
    assert change.second.state is GroupState.MERGED
    assert change.first.successor_id == change.successor.id == change.second.successor_id
    assert change.successor.member_count == group.member_count + other_group.member_count
    assert change.ballot.state is BallotState.APPLIED


def test_merge_cannot_apply_after_composition_change(
    group: VariationGroup,
    other_group: VariationGroup,
    make_ballot: Callable[[VariationTarget, int], VariationBallot],
    reach_threshold: Callable[[VariationBallot], VariationBallot],
    now: datetime,
) -> None:
    ballot = reach_threshold(
        make_ballot(
            MergeTarget.between(group.reference, other_group.reference), VoteThreshold.MERGE
        )
    )
    with pytest.raises(DomainError) as error:
        VariationGovernance.merge(
            first=group.remove_member(),
            second=other_group,
            ballot=ballot,
            successor_id=GroupId(uuid4()),
            decision_id=DecisionId(uuid4()),
            now=now,
        )
    assert error.value.code is GroupViolation.STALE_GROUP


def test_detach_requires_threshold_and_replaces_membership_episode(
    group: VariationGroup,
    membership: GroupMembership,
    make_ballot: Callable[[VariationTarget, int], VariationBallot],
    reach_threshold: Callable[[VariationBallot], VariationBallot],
    now: datetime,
) -> None:
    ballot = make_ballot(DetachTarget(membership_id=membership.id), VoteThreshold.DETACH)
    with pytest.raises(DomainError):
        VariationGovernance.detach(
            group=group,
            membership=membership,
            ballot=ballot,
            singleton_id=GroupId(uuid4()),
            membership_id=MembershipId(uuid4()),
            decision_id=DecisionId(uuid4()),
            now=now,
        )
    change = VariationGovernance.detach(
        group=group,
        membership=membership,
        ballot=reach_threshold(ballot),
        singleton_id=GroupId(uuid4()),
        membership_id=MembershipId(uuid4()),
        decision_id=DecisionId(uuid4()),
        now=now,
    )
    assert not change.closed_membership.active
    assert change.membership.id != membership.id
    assert change.membership.media_id == membership.media_id
    assert change.singleton.member_count == Number.ONE
    assert change.remaining.member_count == group.member_count - Number.ONE
    with pytest.raises(DomainError) as error:
        VariationGovernance.detach(
            group=change.singleton,
            membership=change.membership,
            ballot=reach_threshold(ballot),
            singleton_id=GroupId(uuid4()),
            membership_id=MembershipId(uuid4()),
            decision_id=DecisionId(uuid4()),
            now=now,
        )
    assert error.value.code is GroupViolation.INVALID_MEMBERSHIP


def test_other_member_removal_does_not_reset_this_membership_votes(
    group: VariationGroup,
    membership: GroupMembership,
    make_ballot: Callable[[VariationTarget, int], VariationBallot],
    reach_threshold: Callable[[VariationBallot], VariationBallot],
    now: datetime,
) -> None:
    group = replace(group, member_count=VoteThreshold.DETACH)
    changed = group.remove_member()
    ballot = reach_threshold(
        make_ballot(DetachTarget(membership_id=membership.id), VoteThreshold.DETACH)
    )
    result = VariationGovernance.detach(
        group=changed,
        membership=membership,
        ballot=ballot,
        singleton_id=GroupId(uuid4()),
        membership_id=MembershipId(uuid4()),
        decision_id=DecisionId(uuid4()),
        now=now,
    )
    assert result.ballot.state is BallotState.APPLIED


def test_removing_last_member_empties_group(other_group: VariationGroup) -> None:
    empty = other_group.remove_member()
    assert empty.state is GroupState.EMPTY
    assert empty.member_count == Number.ZERO
