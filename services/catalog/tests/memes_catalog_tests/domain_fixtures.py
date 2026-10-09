from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from memes_catalog.domain.groups import GroupId, GroupMembership, MembershipId, VariationGroup
from memes_catalog.domain.media import (
    AuthorId,
    Dimensions,
    ImageMetadata,
    Media,
    MediaFormat,
    MediaId,
    MediaPolicy,
    OriginalMedia,
)
from memes_catalog.domain.voting import (
    BallotId,
    VariationBallot,
    VariationTarget,
    VoteId,
    VotingRule,
)
from memes_kernel.values import FileRef, Sha256


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def original_file() -> FileRef:
    content = b"original media fixture"
    return FileRef(
        id=uuid4(), sha256=Sha256(value=sha256(content).hexdigest()), size_bytes=len(content)
    )


@pytest.fixture
def image(original_file: FileRef) -> OriginalMedia:
    return OriginalMedia(
        file=original_file,
        format=MediaFormat.PNG,
        metadata=ImageMetadata(dimensions=Dimensions(width=640, height=480)),
    )


@pytest.fixture
def media(image: OriginalMedia, now: datetime) -> Media:
    return Media.publish(
        media_id=MediaId(uuid4()),
        author_id=AuthorId(uuid4()),
        original=image,
        policy=MediaPolicy(),
        now=now,
    )


@pytest.fixture
def group(now: datetime) -> VariationGroup:
    return VariationGroup(id=GroupId(uuid4()), created_at=now, member_count=2)


@pytest.fixture
def other_group(now: datetime) -> VariationGroup:
    return VariationGroup(id=GroupId(uuid4()), created_at=now)


@pytest.fixture
def membership(group: VariationGroup, media: Media, now: datetime) -> GroupMembership:
    return GroupMembership(
        id=MembershipId(uuid4()), media_id=media.id, group_id=group.id, joined_at=now
    )


@pytest.fixture
def make_ballot(now: datetime) -> Callable[[VariationTarget, int], VariationBallot]:
    def factory(target: VariationTarget, threshold: int) -> VariationBallot:
        return VariationBallot(
            id=BallotId(uuid4()),
            target=target,
            rule=VotingRule(threshold=threshold, policy_revision=uuid4()),
            opened_at=now,
        )

    return factory


@pytest.fixture
def reach_threshold(now: datetime) -> Callable[[VariationBallot], VariationBallot]:
    def factory(ballot: VariationBallot) -> VariationBallot:
        for _ in range(ballot.rule.threshold):
            ballot, _ = ballot.cast(
                vote_id=VoteId(uuid4()), voter_id=AuthorId(uuid4()), existing=None, now=now
            )
        return ballot

    return factory
