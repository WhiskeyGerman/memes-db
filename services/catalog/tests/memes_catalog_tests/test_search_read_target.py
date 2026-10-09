"""Выбранное чтение не переназначает durable получателей индексов."""

from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from memes_catalog.application.dto.catalog import SearchIndexTarget
from memes_catalog.infrastructure.configuration import CatalogSettings
from memes_catalog.infrastructure.http.clients import RestDelivery


@pytest.mark.asyncio
@pytest.mark.parametrize("target", list(SearchIndexTarget))
async def test_suggestions_follow_cutover_but_deliveries_keep_stable_targets(
    target: SearchIndexTarget,
    search_deliveries: tuple[dict[SearchIndexTarget, RestDelivery], list[httpx.Request]],
) -> None:
    instances, captured = search_deliveries
    delivery = instances[target]
    await delivery.deliver("search:/internal/search/media-state", {})
    await delivery.deliver("search-shadow:/internal/search/media-state", {})
    await delivery.suggestions(uuid4(), limit=1)
    assert [request.url.host for request in captured[:2]] == [
        "search.invalid",
        "shadow-search.invalid",
    ]
    expected = "shadow-search.invalid" if target is SearchIndexTarget.SHADOW else "search.invalid"
    assert captured[-1].url.host == expected
    assert captured[-1].url.path == "/internal/search/suggestions"
    assert all(request.headers.get("X-Internal-Key") for request in captured)


def test_shadow_read_target_requires_configured_resource(settings: CatalogSettings) -> None:
    with pytest.raises(ValidationError, match="shadow suggestions require"):
        CatalogSettings.model_validate(
            settings.model_dump()
            | {"active_search_index": SearchIndexTarget.SHADOW, "shadow_search_url": None}
        )
