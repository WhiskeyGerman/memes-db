"""Наиболее подходящий фрагмент сохраняет исходный таймкод до API."""

import json
from dataclasses import asdict, replace
from http import HTTPStatus
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_search.application.dto.projections import (
    CatalogMedia,
    EmbeddingVector,
    EmbeddingVersion,
    FeatureKind,
    FeatureProjection,
    FragmentLanguage,
    TextFragment,
)
from memes_search.application.dto.queries import SearchMediaKind, SearchMode, SearchQuery
from memes_search.domain.fragments import MatchedSpan
from memes_search.domain.ranking import MediaCandidate, RankingPolicy
from memes_search.infrastructure.persistence.index import PostgreSQLSearchIndex
from memes_search.presentation.schemas.features import FeatureDeliveryBody
from memes_search_tests.harness import SearchHarness

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("dimensions", [2, 768])
async def test_vector_candidate_returns_span_of_best_fragment(
    database: async_sessionmaker[AsyncSession],
    projection: FeatureProjection,
    dimensions: int,
    vector_version: EmbeddingVersion,
) -> None:
    vector_version = replace(vector_version, dimensions=dimensions)
    index = PostgreSQLSearchIndex(database, fuzzy_threshold=0.15)
    best_span = MatchedSpan(start_ms=90_000, end_ms=95_000)
    projected = replace(
        projection,
        kind=FeatureKind.AUDIO_EMBEDDING,
        text="",
        version_key=json.dumps(asdict(vector_version)),
        vectors=(
            EmbeddingVector(
                values=(0.0, 1.0) + (0.0,) * (dimensions - 2),
                space_id="fixture-space",
                version=vector_version,
                span=MatchedSpan(start_ms=0, end_ms=5_000),
            ),
            EmbeddingVector(
                values=(1.0, 0.0) + (0.0,) * (dimensions - 2),
                space_id="fixture-space",
                version=vector_version,
                span=best_span,
            ),
        ),
    )
    assert await index.apply_feature(projected)
    candidates = await index.vectors(
        SearchQuery(mode=SearchMode.TEXT, text="query"),
        (
            EmbeddingVector(
                values=(1.0,) + (0.0,) * (dimensions - 1),
                space_id="fixture-space",
                version=vector_version,
            ),
        ),
    )
    assert len(candidates) == 1 and candidates[0].matched_span == best_span
    assert RankingPolicy().fuse((candidates,))[0].matched_span == best_span


async def test_text_branches_select_relevant_transcript_interval(
    database: async_sessionmaker[AsyncSession],
    projection: FeatureProjection,
) -> None:
    index = PostgreSQLSearchIndex(database, fuzzy_threshold=0.15)
    span = MatchedSpan(start_ms=30_000, end_ms=35_000)
    projected = replace(
        projection,
        kind=FeatureKind.TRANSCRIPT,
        text="dog cat",
        fragments=(
            TextFragment(
                text="dog",
                language=FragmentLanguage.ENGLISH,
                span=MatchedSpan(start_ms=0, end_ms=2_000),
            ),
            TextFragment(text="funny cat", language=FragmentLanguage.ENGLISH, span=span),
        ),
    )
    assert await index.apply_feature(projected)
    for query in ("cat", "funy"):
        branches = await index.text(SearchQuery(mode=SearchMode.TEXT, text=query))
        matches = [item for branch in branches for item in branch]
        assert matches and all(item.matched_span == span for item in matches)


async def test_group_representative_preserves_own_fragment_in_api(
    harness: SearchHarness,
    client: httpx.AsyncClient,
) -> None:
    group_id, first_id, variant_id = uuid4(), uuid4(), uuid4()
    span = MatchedSpan(start_ms=50_000, end_ms=55_000)
    harness.index.branches = (
        (
            MediaCandidate(media_id=first_id, group_id=group_id, score=0.9, matched_span=span),
            MediaCandidate(
                media_id=variant_id,
                group_id=group_id,
                score=0.1,
                matched_span=MatchedSpan(start_ms=1_000, end_ms=2_000),
            ),
        ),
    )
    for identifier in (first_id, variant_id):
        harness.catalog.items[identifier] = CatalogMedia(
            media_id=identifier,
            group_id=group_id,
            group_revision=1,
            revision=1,
            kind=SearchMediaKind.VIDEO,
            published_at=harness.clock.now(),
        )
    response = await client.post("/v1/search", json={"mode": "text", "text": "cat"})
    assert response.status_code == HTTPStatus.OK
    item = response.json()["results"][0]
    assert item["representative_id"] == str(first_id)
    assert item["matched_span"] == {"start_ms": 50_000, "end_ms": 55_000}


async def test_canonical_callback_preserves_text_and_vector_intervals(
    projection: FeatureProjection,
) -> None:
    common = {
        "event_id": str(projection.event_id),
        "media_id": str(projection.media_id),
        "group_id": str(projection.group_id),
        "group_revision": 1,
        "revision": 1,
        "published_at": projection.published_at.isoformat(),
        "media_kind": "video",
        "cycle_id": str(projection.cycle_id),
        "generation": 1,
        "state": "available",
        "feature_version": {
            "model": "fixture",
            "weights": "fixture",
            "task_adapter": "fixture",
            "preprocessing": "fixture",
            "space": "fixture-space",
            "dimensions": 2,
        },
    }
    interval = {"start_ms": 42_000, "end_ms": 43_000}
    text_projection = FeatureDeliveryBody.model_validate(
        {
            **common,
            "kind": "transcript",
            "result": {"fragments": [{"text": "funny cat", "language": "en", "span": interval}]},
        }
    ).projection()
    vector_projection = FeatureDeliveryBody.model_validate(
        {
            **common,
            "kind": "visual_embedding",
            "result": {"items": [{"values": [1.0, 0.0], "span": interval}]},
        }
    ).projection()
    expected = MatchedSpan(start_ms=42_000, end_ms=43_000)
    assert text_projection.fragments[0].span == expected
    assert vector_projection.vectors[0].span == expected
