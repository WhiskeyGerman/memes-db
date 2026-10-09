"""Группы и голоса: настоящие блокировки PostgreSQL и эпизоды членства."""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import select

from memes_catalog.application.dto.catalog import Actor
from memes_catalog.domain.groups import GroupState
from memes_catalog.domain.voting import BallotState
from memes_catalog.infrastructure.persistence.models import (
    BallotRow,
    DecisionRow,
    GroupRow,
    MembershipRow,
)
from memes_catalog_tests.conftest import CatalogHarness
from memes_platform.application.errors import ApplicationError, ErrorCode

pytestmark = pytest.mark.integration


async def test_concurrent_distinct_votes_apply_merge_once(
    harness: CatalogHarness,
    actor: Actor,
    png: Callable[[int], bytes],
) -> None:
    first = await harness.publish(actor, png(3))
    second = await harness.publish(actor, png(4))
    harness.variations.merge_threshold = 2

    async def cast(participant: Actor) -> object:
        return await harness.variations.merge(
            actor=participant,
            first_id=first.group.id,
            second_id=second.group.id,
            first_revision=1,
            second_revision=1,
            now=harness.now,
        )

    outcomes = await asyncio.gather(cast(actor), cast(replace(actor, user_id=uuid4())))
    assert len(outcomes) == 2
    async with harness.sessions() as session:
        decisions = (await session.scalars(select(DecisionRow))).all()
        active_groups = (
            await session.scalars(select(GroupRow).where(GroupRow.state == GroupState.ACTIVE))
        ).all()
        active_members = (
            await session.scalars(select(MembershipRow).where(MembershipRow.ended_at.is_(None)))
        ).all()
        assert len(decisions) == 1
        assert len(active_groups) == 1 and active_groups[0].member_count == 2
        assert len(active_members) == 2
        assert {item.group_id for item in active_members} == {active_groups[0].id}


async def test_parallel_repeat_vote_is_counted_once(
    harness: CatalogHarness,
    actor: Actor,
    png: Callable[[int], bytes],
) -> None:
    first = await harness.publish(actor, png(5))
    second = await harness.publish(actor, png(6))
    harness.variations.merge_threshold = 3

    async def cast() -> object:
        return await harness.variations.merge(
            actor=actor,
            first_id=first.group.id,
            second_id=second.group.id,
            first_revision=1,
            second_revision=1,
            now=harness.now,
        )

    await asyncio.gather(cast(), cast())
    async with harness.sessions() as session:
        ballots = (await session.scalars(select(BallotRow))).all()
        assert len(ballots) == 1 and ballots[0].active_votes == 1
        assert not (await session.scalars(select(DecisionRow))).all()


async def test_detach_targets_current_membership_and_reattach_resets_votes(
    harness: CatalogHarness,
    actor: Actor,
    png: Callable[[int], bytes],
) -> None:
    first = await harness.publish(actor, png(7))
    second = await harness.publish(actor, png(8))
    await harness.merge(first, second, actor)
    merged_first = await harness.catalog.get(first.media.id)
    membership_id = merged_first.membership.id
    await harness.variations.detach(actor=actor, membership_id=membership_id, now=harness.now)
    result = await harness.variations.detach(
        actor=replace(actor, user_id=uuid4()), membership_id=membership_id, now=harness.now
    )
    assert result.applied
    detached = await harness.catalog.get(first.media.id)
    remaining = await harness.catalog.get(second.media.id)
    assert detached.group.member_count == remaining.group.member_count == 1
    await harness.merge(detached, remaining, actor)
    attached = await harness.catalog.get(first.media.id)
    assert attached.membership.id != membership_id
    with pytest.raises(ApplicationError) as stale:
        await harness.variations.detach(actor=actor, membership_id=membership_id, now=harness.now)
    assert stale.value.code is ErrorCode.STALE_STATE
    fresh = await harness.variations.detach(
        actor=actor, membership_id=attached.membership.id, now=harness.now
    )
    assert fresh.active_votes == 1
    assert not fresh.applied


async def test_deleting_other_member_retains_detach_votes(
    harness: CatalogHarness,
    actor: Actor,
    png: Callable[[int], bytes],
) -> None:
    first, second, third = [await harness.publish(actor, png(seed)) for seed in (9, 10, 11)]
    await harness.merge(first, second, actor)
    joined = await harness.catalog.get(first.media.id)
    await harness.merge(joined, third, actor)
    target = await harness.catalog.get(first.media.id)
    await harness.variations.detach(
        actor=actor, membership_id=target.membership.id, now=harness.now
    )
    await harness.catalog.delete(media_id=third.media.id, actor=actor, now=harness.now)
    applied = await harness.variations.detach(
        actor=replace(actor, user_id=uuid4()), membership_id=target.membership.id, now=harness.now
    )
    assert applied.applied
    async with harness.sessions() as session:
        ballot = await session.get(BallotRow, applied.ballot_id)
        assert ballot is not None and ballot.state == BallotState.APPLIED


async def test_vote_can_be_withdrawn_before_threshold(
    harness: CatalogHarness,
    actor: Actor,
    png: Callable[[int], bytes],
) -> None:
    first = await harness.publish(actor, png(12))
    second = await harness.publish(actor, png(13))
    harness.variations.merge_threshold = 2
    outcome = await harness.variations.merge(
        actor=actor,
        first_id=first.group.id,
        second_id=second.group.id,
        first_revision=1,
        second_revision=1,
        now=harness.now,
    )
    result = await harness.variations.withdraw(
        actor=actor, ballot_id=outcome.ballot_id, now=harness.now
    )
    assert result.active_votes == 0
    assert not result.applied
