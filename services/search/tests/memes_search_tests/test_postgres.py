"""Гарантии реального PostgreSQL: поколения, tombstone, квоты и расширения."""

import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_kernel.values import Number
from memes_search.application.dto.delivery import OutboxAction
from memes_search.application.dto.projections import (
    EmbeddingVector,
    EmbeddingVersion,
    FeatureKind,
    FeatureProjection,
    MediaStateChange,
    ProjectionState,
)
from memes_search.application.dto.queries import SearchMode, SearchQuery
from memes_search.application.dto.samples import PrivateSample, SampleState
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.infrastructure.persistence.index import PostgreSQLSearchIndex
from memes_search.infrastructure.persistence.models import OutboxRow
from memes_search.infrastructure.persistence.samples import PostgreSQLSamples

pytestmark = pytest.mark.integration


async def test_real_fulltext_and_trigram_search(
    database: async_sessionmaker[AsyncSession],
    projection: FeatureProjection,
) -> None:
    index = PostgreSQLSearchIndex(database, fuzzy_threshold=0.15)
    assert await index.apply_feature(projection)
    for query in ("кот", "cats", "funy"):
        branches = await index.text(SearchQuery(mode=SearchMode.TEXT, text=query))
        assert any(
            candidate.media_id == projection.media_id for branch in branches for candidate in branch
        )


async def test_tombstone_received_before_result_never_resurrects_media(
    database: async_sessionmaker[AsyncSession],
    projection: FeatureProjection,
) -> None:
    index = PostgreSQLSearchIndex(database, fuzzy_threshold=0.15)
    assert await index.apply_media_state(
        MediaStateChange(event_id=uuid4(), media_id=projection.media_id, revision=2, active=False)
    )
    assert not await index.apply_feature(projection)
    assert not await index.apply_media_state(
        MediaStateChange(
            event_id=uuid4(),
            media_id=projection.media_id,
            revision=3,
            active=True,
            group_id=projection.group_id,
            group_revision=1,
            kind=projection.media_kind,
            published_at=projection.published_at,
        )
    )
    assert not any(await index.text(SearchQuery(mode=SearchMode.TEXT, text="cat")))


async def test_duplicate_and_older_generation_cannot_replace_features(
    database: async_sessionmaker[AsyncSession],
    projection: FeatureProjection,
) -> None:
    index = PostgreSQLSearchIndex(database, fuzzy_threshold=0.15)
    assert await index.apply_feature(replace(projection, generation=2, text="новый кот"))
    assert not await index.apply_feature(projection)
    assert not await index.apply_feature(replace(projection, event_id=uuid4()))
    features = await index.features_for(projection.media_id)
    assert features.text == "новый кот"


async def test_late_feature_does_not_overwrite_new_membership(
    database: async_sessionmaker[AsyncSession],
    projection: FeatureProjection,
) -> None:
    index = PostgreSQLSearchIndex(database, fuzzy_threshold=0.15)
    new_group = uuid4()
    assert await index.apply_media_state(
        MediaStateChange(
            event_id=uuid4(),
            media_id=projection.media_id,
            revision=3,
            active=True,
            group_id=new_group,
            group_revision=2,
            kind=projection.media_kind,
            published_at=projection.published_at,
        )
    )
    assert await index.apply_feature(projection)
    branches = await index.text(SearchQuery(mode=SearchMode.TEXT, text="cat"))
    assert all(item.group_id == new_group for branch in branches for item in branch)


async def test_real_pgvector_spaces_do_not_cross(
    database: async_sessionmaker[AsyncSession],
    projection: FeatureProjection,
    vector_version: EmbeddingVersion,
) -> None:
    index = PostgreSQLSearchIndex(database, fuzzy_threshold=0.15)
    projected = replace(
        projection,
        kind=FeatureKind.VISUAL_EMBEDDING,
        text="",
        version_key=json.dumps(asdict(vector_version)),
        vectors=(
            EmbeddingVector(values=(1.0, 0.0), space_id="fixture-space", version=vector_version),
        ),
    )
    assert await index.apply_feature(projected)
    query = SearchQuery(mode=SearchMode.TEXT, text="query")
    assert await index.vectors(
        query,
        (EmbeddingVector(values=(1.0, 0.0), space_id="fixture-space", version=vector_version),),
    )
    assert not await index.vectors(
        query,
        (
            EmbeddingVector(
                values=(1.0, 0.0),
                space_id="another-space",
                version=replace(vector_version, space="another-space"),
            ),
        ),
    )


async def test_sample_quota_and_delivery_are_transactional(
    database: async_sessionmaker[AsyncSession],
    now: datetime,
) -> None:
    repository = PostgreSQLSamples(database)
    owner = uuid4()
    sample = PrivateSample(
        id=uuid4(),
        owner_id=owner,
        object_key="samples/fixture/file",
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    await repository.reserve(sample, 1)
    with pytest.raises(SearchError) as error:
        await repository.reserve(replace(sample, id=uuid4()), 1)
    assert error.value.code is SearchErrorCode.SAMPLE_LIMIT
    await repository.save(replace(sample, state=SampleState.CANCELLED), enqueue=True)
    await repository.reserve(replace(sample, id=uuid4()), 1)
    async with database() as session:
        rows = (
            await session.scalars(select(OutboxRow).where(OutboxRow.action == OutboxAction.CANCEL))
        ).all()
    assert len(rows) == Number.ONE
    assert rows[0].sample_id is None
    assert rows[0].body == {"sample_id": str(sample.id)}


async def test_pending_generation_can_transition_only_in_same_cycle(
    database: async_sessionmaker[AsyncSession],
    projection: FeatureProjection,
) -> None:
    index = PostgreSQLSearchIndex(database, fuzzy_threshold=0.15)
    pending = replace(projection, event_id=uuid4(), state=ProjectionState.PENDING, text="")
    assert await index.apply_feature(pending)
    assert not await index.apply_feature(replace(projection, event_id=uuid4(), cycle_id=uuid4()))
    assert await index.apply_feature(projection)
    assert not await index.apply_feature(replace(pending, event_id=uuid4()))
    assert (await index.features_for(projection.media_id)).text == projection.text


async def test_hnsw_768_returns_original_cosine_score(
    database: async_sessionmaker[AsyncSession],
    projection: FeatureProjection,
    vector_version: EmbeddingVersion,
) -> None:
    index = PostgreSQLSearchIndex(database, fuzzy_threshold=0.15)
    vector_version = replace(vector_version, dimensions=768, space="fixture-768")
    values = (1.0,) + (0.0,) * 767
    projected = replace(
        projection,
        kind=FeatureKind.VISUAL_EMBEDDING,
        text="",
        version_key=json.dumps(asdict(vector_version)),
        vectors=(EmbeddingVector(values=values, space_id="fixture-768", version=vector_version),),
    )
    assert await index.apply_feature(projected)
    result = await index.vectors(
        SearchQuery(mode=SearchMode.TEXT, text="query"),
        (EmbeddingVector(values=values, space_id="fixture-768", version=vector_version),),
    )
    assert result[0].media_id == projection.media_id
    assert result[0].score == pytest.approx(1.0)


@pytest.mark.parametrize("dimensions", [2, 768])
async def test_variation_neighbors_require_compatible_weights_and_report_modalities(
    database: async_sessionmaker[AsyncSession],
    projection: FeatureProjection,
    dimensions: int,
) -> None:
    index = PostgreSQLSearchIndex(database, fuzzy_threshold=0.15)
    version = {
        "model": "fixture-model",
        "weights": "fixture-weights",
        "task_adapter": "retrieval-v1",
        "preprocessing": "fixture-v1",
        "space": "fixture-space",
        "dimensions": dimensions,
    }
    source = replace(
        projection,
        kind=FeatureKind.VISUAL_EMBEDDING,
        text="",
        version_key=json.dumps(version),
        vectors=(
            EmbeddingVector(values=(1.0,) + (0.0,) * (dimensions - 1), space_id="fixture-space"),
        ),
    )
    target = replace(
        source,
        event_id=uuid4(),
        media_id=uuid4(),
        group_id=uuid4(),
        cycle_id=uuid4(),
        kind=FeatureKind.AUDIO_EMBEDDING,
        version_key=json.dumps({**version, "task_adapter": "retrieval-v1"}),
        vectors=(
            EmbeddingVector(
                values=(0.9, 0.1) + (0.0,) * (dimensions - 2), space_id="fixture-space"
            ),
        ),
    )
    incompatible = replace(
        target,
        event_id=uuid4(),
        media_id=uuid4(),
        group_id=uuid4(),
        cycle_id=uuid4(),
        version_key=json.dumps({**version, "weights": "different-weights"}),
    )
    incompatible_adapter = replace(
        target,
        event_id=uuid4(),
        media_id=uuid4(),
        group_id=uuid4(),
        cycle_id=uuid4(),
        version_key=json.dumps({**version, "task_adapter": "clustering-v1"}),
    )
    for item in (source, target, incompatible, incompatible_adapter):
        assert await index.apply_feature(item)
    matches = await index.variation_matches(source.media_id)
    assert len(matches) == 1 and matches[0].media_id == target.media_id
    assert matches[0].source_kind is FeatureKind.VISUAL_EMBEDDING
    assert matches[0].target_kind is FeatureKind.AUDIO_EMBEDDING
    assert matches[0].model_revision == "fixture-model@fixture-weights"
    assert matches[0].score > 0.9
