"""Лента не теряет равное время публикаций и продолжает после скрытых авторов."""

from collections.abc import Callable
from dataclasses import replace
from http import HTTPStatus
from uuid import UUID, uuid4

import httpx
import pytest

from memes_catalog.application.dto.catalog import Actor, FeedCursor
from memes_catalog_tests.conftest import CatalogHarness

pytestmark = pytest.mark.integration


async def test_feed_cursor_preserves_equal_publication_timestamps(
    client: httpx.AsyncClient,
    harness: CatalogHarness,
    actor: Actor,
    png: Callable[[int], bytes],
) -> None:
    publications = [await harness.publish(actor, png(seed)) for seed in range(32, 38)]
    expected = sorted((view.media.id for view in publications), key=lambda item: item.int)
    delivered: list[UUID] = []
    parameters = {"limit": "2"}
    for _ in range(5):
        response = await client.get("/v1/media", params=parameters)
        assert response.status_code == HTTPStatus.OK
        body = response.json()
        delivered.extend(UUID(item["media_id"]) for item in body["items"])
        if body["next_before"] is None:
            break
        parameters = {
            "limit": "2",
            "before": body["next_before"],
            "before_id": body["next_before_id"],
        }
    assert delivered == expected


async def test_feed_fills_filtered_boundary_and_advances_from_last_scanned_item(
    harness: CatalogHarness,
    actor: Actor,
    png: Callable[[int], bytes],
) -> None:
    publications = [
        await harness.publish(replace(actor, user_id=uuid4()), png(seed)) for seed in range(38, 44)
    ]
    ordered = sorted(publications, key=lambda item: item.media.id.int)
    for view in ordered[:3]:
        harness.identity.deleted[view.media.author_id] = True
    first = await harness.catalog.feed(before=None, limit=2)
    assert [item.media.id for item in first.items] == [item.media.id for item in ordered[3:5]]
    assert first.next_cursor == FeedCursor(published_at=harness.now, media_id=ordered[4].media.id)
    next_page = await harness.catalog.feed(before=first.next_cursor, limit=2)
    assert [item.media.id for item in next_page.items] == [ordered[-1].media.id]
    assert next_page.next_cursor is None


async def test_feed_rejects_id_cursor_without_timestamp(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/media", params={"before_id": str(uuid4())})
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
