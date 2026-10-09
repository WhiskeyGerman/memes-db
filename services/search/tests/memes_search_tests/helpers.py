"""Общие образцы и стримы для сценарных тестов."""

from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import uuid4

from memes_search.application.dto.projections import CatalogMedia
from memes_search.application.dto.queries import SearchMediaKind
from memes_search.domain.ranking import MediaCandidate
from memes_search_tests.harness import SearchHarness


def populate(harness: SearchHarness) -> tuple[CatalogMedia, CatalogMedia]:
    first = CatalogMedia(
        media_id=uuid4(),
        group_id=uuid4(),
        group_revision=1,
        revision=1,
        kind=SearchMediaKind.IMAGE,
        published_at=harness.clock.now(),
    )
    second = replace(first, media_id=uuid4(), group_id=uuid4())
    harness.catalog.items = {item.media_id: item for item in (first, second)}
    harness.index.branches = (
        (
            MediaCandidate(media_id=first.media_id, group_id=first.group_id, score=1.0),
            MediaCandidate(media_id=second.media_id, group_id=second.group_id, score=0.5),
        ),
    )
    return first, second


async def chunks() -> AsyncIterator[bytes]:
    yield b"private sample"
