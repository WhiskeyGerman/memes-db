"""Обычный поиск исключает модели с несовместимыми весами и pipeline."""

import json
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_search.application.dto.projections import (
    EmbeddingVector,
    EmbeddingVersion,
    FeatureKind,
    FeatureProjection,
)
from memes_search.application.dto.queries import SearchMode, SearchQuery
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.infrastructure.persistence.index import PostgreSQLSearchIndex

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("dimensions", [2, 768])
@pytest.mark.parametrize("changed_field", ["model", "weights", "preprocessing", "task_adapter"])
async def test_query_and_cached_sample_require_full_compatibility(
    database: async_sessionmaker[AsyncSession],
    projection: FeatureProjection,
    vector_version: EmbeddingVersion,
    dimensions: int,
    changed_field: str,
) -> None:
    index = PostgreSQLSearchIndex(database, fuzzy_threshold=0.15)
    version = replace(vector_version, dimensions=dimensions)
    values = (1.0,) + (0.0,) * (dimensions - 1)
    source = replace(
        projection,
        kind=FeatureKind.VISUAL_EMBEDDING,
        text="",
        version_key=json.dumps(asdict(version)),
        vectors=(EmbeddingVector(values=values, space_id=version.space, version=version),),
    )
    assert await index.apply_feature(source)
    incompatible_version = asdict(version)
    incompatible_version[changed_field] = "another-version"
    incompatible = replace(
        source,
        event_id=uuid4(),
        media_id=uuid4(),
        group_id=uuid4(),
        cycle_id=uuid4(),
        version_key=json.dumps(incompatible_version),
    )
    assert await index.apply_feature(incompatible)
    query = SearchQuery(mode=SearchMode.TEXT, text="query")
    candidates = await index.vectors(
        query, (EmbeddingVector(values=values, space_id=version.space, version=version),)
    )
    assert [item.media_id for item in candidates] == [source.media_id]
    cached = await index.features_for(source.media_id)
    assert cached.vectors[0].version == version
    sample_candidates = await index.vectors(query, cached.vectors)
    assert [item.media_id for item in sample_candidates] == [source.media_id]
    with pytest.raises(SearchError) as error:
        await index.vectors(query, (EmbeddingVector(values=values, space_id=version.space),))
    assert error.value.code is SearchErrorCode.UNAVAILABLE
