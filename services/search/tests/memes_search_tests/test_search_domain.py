from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest

from memes_kernel.errors import DomainError
from memes_kernel.values import Number, Revision
from memes_search.application.search import (
    GroupSnapshot,
    SearchError,
    SearchErrorCode,
    SearchLimit,
    SearchMode,
    SearchQuery,
    SearchSession,
)
from memes_search.domain.ranking import MediaCandidate, RankedGroup, RankingPolicy, RankingViolation


def test_fragments_and_variants_do_not_boost_group() -> None:
    group_id, other_id = uuid4(), uuid4()
    strongest = MediaCandidate(media_id=uuid4(), group_id=group_id, score=1.0)
    weak_variant = MediaCandidate(media_id=uuid4(), group_id=group_id, score=0.1)
    competitor = MediaCandidate(media_id=uuid4(), group_id=other_id, score=0.5)
    policy = RankingPolicy()
    original = policy.fuse(((strongest, competitor),))
    expanded = policy.fuse(((strongest, strongest, strongest, competitor, weak_variant),))
    assert expanded == original
    assert expanded[Number.ZERO].representative_id == strongest.media_id


def test_rrf_combines_independent_retrieval_branches() -> None:
    first = MediaCandidate(media_id=uuid4(), group_id=uuid4(), score=1.0)
    second = MediaCandidate(media_id=uuid4(), group_id=uuid4(), score=0.5)
    result = RankingPolicy().fuse(((first, second), (second,)))
    assert result[Number.ZERO].representative_id == second.media_id


def test_exact_match_becomes_group_representative_and_first_result() -> None:
    group_id = uuid4()
    best = MediaCandidate(media_id=uuid4(), group_id=group_id, score=1.0)
    exact = MediaCandidate(media_id=uuid4(), group_id=group_id, score=0.1)
    competitor = MediaCandidate(media_id=uuid4(), group_id=uuid4(), score=0.5)
    result = RankingPolicy().fuse(((best, competitor, exact),), exact_media_id=exact.media_id)
    assert result[Number.ZERO].representative_id == exact.media_id


def test_equal_relevance_is_stable_by_id() -> None:
    first = MediaCandidate(media_id=UUID(int=1), group_id=UUID(int=3), score=1.0)
    second = MediaCandidate(media_id=UUID(int=2), group_id=UUID(int=4), score=1.0)
    policy = RankingPolicy()
    assert policy.fuse(((first, second),)) == policy.fuse(((second, first),))


def test_contradictory_membership_is_rejected() -> None:
    candidate = MediaCandidate(media_id=uuid4(), group_id=uuid4(), score=1.0)
    with pytest.raises(DomainError) as error:
        RankingPolicy().fuse(((candidate,), (replace(candidate, group_id=uuid4()),)))
    assert error.value.code is RankingViolation.INCONSISTENT_MEMBERSHIP


def test_private_sample_requires_owner() -> None:
    with pytest.raises(SearchError):
        SearchQuery(mode=SearchMode.SAMPLE)


def test_changed_group_invalidates_search_session(now: datetime) -> None:
    snapshot = GroupSnapshot(group_id=uuid4(), composition_revision=Revision())
    session = SearchSession(
        id=uuid4(),
        query=SearchQuery(text="кот"),
        created_at=now,
        expires_at=now + timedelta(seconds=SearchLimit.SESSION_SECONDS),
        results=(RankedGroup(group_id=snapshot.group_id, representative_id=uuid4(), score=1.0),),
        groups=(snapshot,),
    )
    session.require_current(now=now, groups=(snapshot,))
    with pytest.raises(SearchError) as changed:
        session.require_current(
            now=now, groups=(replace(snapshot, composition_revision=Revision().next()),)
        )
    assert changed.value.code is SearchErrorCode.GROUPS_CHANGED
    with pytest.raises(SearchError) as expired:
        session.require_current(now=session.expires_at, groups=(snapshot,))
    assert expired.value.code is SearchErrorCode.SESSION_EXPIRED
