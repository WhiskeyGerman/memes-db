"""Предложения вариаций: пороги пары, версия модели, лимит и живой каталог."""

from dataclasses import replace
from http import HTTPStatus
from types import SimpleNamespace
from uuid import uuid4

import httpx
from fastapi import FastAPI

from memes_search.application.dto.projections import CatalogMedia, FeatureKind
from memes_search.application.dto.queries import SearchMediaKind
from memes_search.application.dto.suggestions import SimilarityThreshold, VariationMatch
from memes_search.application.use_cases.suggestions import SuggestionService
from memes_search_tests.harness import SearchHarness


async def test_modality_model_threshold_and_live_group_filter(harness: SearchHarness) -> None:
    source_id, own_group, accepted_id, low_id, stale_id = (uuid4() for _ in range(5))
    source = CatalogMedia(
        media_id=source_id,
        group_id=own_group,
        group_revision=1,
        revision=1,
        kind=SearchMediaKind.IMAGE,
        published_at=harness.clock.now(),
    )
    accepted = replace(source, media_id=accepted_id, group_id=uuid4())
    low = replace(source, media_id=low_id, group_id=uuid4())
    harness.catalog.items = {item.media_id: item for item in (source, accepted, low)}
    template = VariationMatch(
        media_id=accepted_id,
        score=0.85,
        space_id="fixture-space",
        model_revision="fixture-model@fixture-weights",
        source_kind=FeatureKind.VISUAL_EMBEDDING,
        target_kind=FeatureKind.AUDIO_EMBEDDING,
    )
    harness.index.matches = (
        template,
        replace(template, media_id=low_id, score=0.83),
        replace(template, media_id=stale_id, score=1.0),
        replace(template, media_id=source_id, score=1.0),
    )
    service = SuggestionService(
        index=harness.index,
        catalog=harness.catalog,
        threshold=0.8,
        rules=(
            SimilarityThreshold(
                space_id=template.space_id,
                model_revision=template.model_revision,
                source_kind=template.source_kind,
                target_kind=template.target_kind,
                threshold=0.84,
            ),
        ),
    )
    suggestions = await service.execute(source_id, 10)
    assert len(suggestions) == 1
    assert suggestions[0].media_id == accepted_id and suggestions[0].score == template.score
    harness.index.matches = (replace(template, model_revision="other-model@other-weights"),)
    assert len(await service.execute(source_id, 10)) == 1


async def test_suggestions_collapse_groups_and_never_exceed_ten(harness: SearchHarness) -> None:
    source_id = uuid4()
    source = CatalogMedia(
        media_id=source_id,
        group_id=uuid4(),
        group_revision=1,
        revision=1,
        kind=SearchMediaKind.IMAGE,
        published_at=harness.clock.now(),
    )
    harness.catalog.items[source_id] = source
    items = tuple(replace(source, media_id=uuid4(), group_id=uuid4()) for _ in range(15))
    duplicate = replace(items[0], media_id=uuid4())
    harness.catalog.items.update({item.media_id: item for item in (*items, duplicate)})
    harness.index.matches = tuple(
        VariationMatch(
            media_id=item.media_id,
            score=0.85,
            space_id="fixture-space",
            model_revision="fixture-model@fixture-weights",
            source_kind=FeatureKind.VISUAL_EMBEDDING,
            target_kind=FeatureKind.VISUAL_EMBEDDING,
        )
        for item in (*items, duplicate)
    )
    results = await harness.services.suggestions.execute(source_id, 100)
    assert len(results) == 10 and len({item.group_id for item in results}) == 10


async def test_http_suggestions_limit_is_ten(client: httpx.AsyncClient, app: FastAPI) -> None:
    app.state.settings = SimpleNamespace(internal_api_key="test-internal-key")
    response = await client.post(
        "/internal/search/suggestions",
        json={"media_id": str(uuid4()), "limit": 11},
        headers={"X-Internal-Key": "test-internal-key"},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
