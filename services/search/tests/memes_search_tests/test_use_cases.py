"""Проверка продуктовых сценариев независимо от транспорта и БД."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from memes_search.application.dto.execution import SearchPending
from memes_search.application.dto.queries import SearchMode, SearchQuery
from memes_search.application.dto.samples import InferenceState
from memes_search.application.dto.sessions import SearchPage
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search_tests.harness import SearchHarness
from memes_search_tests.helpers import chunks, populate


async def test_live_group_mapping_is_taken_from_catalog(harness: SearchHarness) -> None:
    first, second = populate(harness)
    harness.catalog.items[second.media_id] = replace(second, group_id=first.group_id)
    result = await harness.services.search.execute(
        SearchQuery(mode=SearchMode.TEXT, text="cat"), uuid4()
    )
    assert isinstance(result, SearchPage)
    assert len(result.results) == 1
    assert result.results[0].group_id == first.group_id


async def test_deletion_does_not_shift_snapshot_cursor(harness: SearchHarness) -> None:
    first, second = populate(harness)
    result = await harness.services.search.execute(
        SearchQuery(mode=SearchMode.TEXT, text="cat", page_size=1), uuid4()
    )
    assert isinstance(result, SearchPage)
    harness.catalog.items.pop(first.media_id)
    page = await harness.services.search.page(result.session_id, None, 1, 1)
    assert page.results[0].representative_id == second.media_id
    assert page.next_offset is None


async def test_changed_composition_invalidates_existing_pages(harness: SearchHarness) -> None:
    first, _ = populate(harness)
    result = await harness.services.search.execute(
        SearchQuery(mode=SearchMode.TEXT, text="cat"), uuid4()
    )
    assert isinstance(result, SearchPage)
    harness.catalog.items[first.media_id] = replace(first, group_revision=2)
    with pytest.raises(SearchError) as error:
        await harness.services.search.page(result.session_id, None, 0, 20)
    assert error.value.code is SearchErrorCode.GROUPS_CHANGED


async def test_semantic_pending_is_preserved_and_resumed(harness: SearchHarness) -> None:
    populate(harness)
    harness.inference.state = InferenceState.PENDING
    request_id = uuid4()
    result = await harness.services.search.execute(
        SearchQuery(mode=SearchMode.SEMANTIC, text="cat"), request_id
    )
    assert isinstance(result, SearchPending)
    assert not harness.sessions.items
    harness.inference.state = InferenceState.SUCCEEDED
    ready = await harness.services.search.resume(request_id, None)
    assert isinstance(ready, SearchPage)
    assert not ready.degraded
    assert harness.inference.queries == 1


async def test_semantic_outage_is_not_silently_replaced_by_text(harness: SearchHarness) -> None:
    populate(harness)
    harness.inference.failure = True
    with pytest.raises(SearchError) as error:
        await harness.services.search.execute(
            SearchQuery(mode=SearchMode.SEMANTIC, text="cat"), uuid4()
        )
    assert error.value.code is SearchErrorCode.UNAVAILABLE
    assert not harness.sessions.items


async def test_hybrid_outage_explicitly_marks_partial_results(harness: SearchHarness) -> None:
    populate(harness)
    harness.inference.failure = True
    result = await harness.services.search.execute(SearchQuery(text="cat"), uuid4())
    assert isinstance(result, SearchPage)
    assert result.degraded
    assert len(result.results) == 2


async def test_expired_private_text_is_not_resumed(harness: SearchHarness) -> None:
    request_id = uuid4()
    harness.inference.state = InferenceState.PENDING
    await harness.services.search.execute(
        SearchQuery(mode=SearchMode.SEMANTIC, text="cat"), request_id
    )
    harness.clock.value += timedelta(minutes=11)
    with pytest.raises(SearchError) as error:
        await harness.services.search.resume(request_id, None)
    assert error.value.code is SearchErrorCode.SESSION_EXPIRED


async def test_private_sample_never_creates_catalog_item(harness: SearchHarness) -> None:
    sample = await harness.services.samples.upload(harness.identity.user_id, chunks())
    assert not harness.catalog.items
    assert sample.id in harness.samples.items
    assert sample.id in harness.samples.queued
    assert sample.object_key in harness.storage.items


async def test_invalid_decoder_result_does_not_enqueue_sample(harness: SearchHarness) -> None:
    harness.catalog.invalid_file = True
    with pytest.raises(SearchError) as error:
        await harness.services.samples.upload(harness.identity.user_id, chunks())
    assert error.value.code is SearchErrorCode.INVALID_SAMPLE
    assert not harness.samples.queued


async def test_sample_owner_is_not_supplied_by_http_client(harness: SearchHarness) -> None:
    sample = await harness.services.samples.upload(harness.identity.user_id, chunks())
    with pytest.raises(SearchError) as error:
        await harness.services.samples.get(sample.id, uuid4())
    assert error.value.code is SearchErrorCode.FORBIDDEN


async def test_sample_cancel_revokes_file_before_async_cleanup(harness: SearchHarness) -> None:
    sample = await harness.services.samples.upload(harness.identity.user_id, chunks())
    await harness.services.samples.cancel(sample.id, harness.identity.user_id)
    assert sample.object_key not in harness.storage.items
    with pytest.raises(SearchError):
        await harness.services.samples.download(sample.id, harness.identity.user_id)


async def test_private_signed_link_never_outlives_sixty_seconds_or_sample(
    harness: SearchHarness,
) -> None:
    sample = await harness.services.samples.upload(harness.identity.user_id, chunks())
    await harness.services.samples.download(sample.id, harness.identity.user_id)
    assert harness.storage.signed_lifetimes[-1] == 60
    harness.clock.value += timedelta(seconds=550)
    await harness.services.samples.download(sample.id, harness.identity.user_id)
    assert harness.storage.signed_lifetimes[-1] == 50
    harness.clock.value += timedelta(seconds=49, milliseconds=500)
    with pytest.raises(SearchError) as expired:
        await harness.services.samples.download(sample.id, harness.identity.user_id)
    assert expired.value.code is SearchErrorCode.NOT_FOUND
