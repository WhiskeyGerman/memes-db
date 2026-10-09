"""Терминальные переходы и ошибки не удерживают глобальные слоты."""

import asyncio
from dataclasses import replace
from datetime import datetime
from http import HTTPStatus
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from memes_search.application.dto.projections import ProjectionState
from memes_search.application.dto.queries import SearchLimit
from memes_search.application.dto.samples import SampleFeatureChange, SampleState
from memes_search.application.dto.sessions import SearchSessionPolicy
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.application.use_cases.samples import SampleService
from memes_search.infrastructure.configuration import Settings
from memes_search_tests.fakes import PausingStorage, ReceiveSpy, Samples
from memes_search_tests.harness import SearchHarness
from memes_search_tests.helpers import chunks


class CleanupFailureSamples(Samples):
    async def schedule_cleanup(self, object_key: str, available_at: datetime) -> None:
        raise SearchError(SearchErrorCode.UNAVAILABLE)


@pytest.fixture
def pausing_sample_service(harness: SearchHarness) -> tuple[SampleService, PausingStorage]:
    storage = PausingStorage()
    service = SampleService(
        repository=harness.samples,
        admission=harness.admission,
        storage=storage,
        inspector=harness.catalog,
        catalog=harness.catalog,
        clock=harness.clock,
        policy=SearchSessionPolicy(),
        index=harness.index,
        embedding_version=harness.version,
    )
    return service, storage


async def test_admission_unavailability_rejects_before_multipart_body_is_read(
    harness: SearchHarness,
    spy_client: httpx.AsyncClient,
    receive_spy: ReceiveSpy,
) -> None:
    harness.admission.unavailable = True
    response = await spy_client.post(
        "/v1/search/samples",
        headers={"Authorization": f"Bearer {harness.identity.user_id}"},
        files={"file": ("private.png", b"private body")},
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert receive_spy.calls == 0
    assert not harness.samples.items and not harness.storage.items


@pytest.mark.parametrize("terminal", [SampleState.READY, SampleState.FAILED])
async def test_terminal_projection_releases_slot_even_when_replayed(
    harness: SearchHarness,
    terminal: SampleState,
) -> None:
    sample = await harness.services.samples.upload(harness.identity.user_id, chunks())
    assert sample.id in harness.admission.items
    change = SampleFeatureChange(
        event_id=uuid4(),
        sample_id=sample.id,
        kind="ocr",
        generation=1,
        state=ProjectionState.AVAILABLE
        if terminal is SampleState.READY
        else ProjectionState.FAILED,
        text="ready" if terminal is SampleState.READY else "",
        sample_state=terminal,
    )
    await harness.services.projections.private_feature(change)
    await harness.services.projections.private_feature(change)
    assert sample.id not in harness.admission.items


async def test_client_disconnect_releases_slot_and_expires_partial_upload(
    harness: SearchHarness,
    pausing_sample_service: tuple[SampleService, PausingStorage],
) -> None:
    service, storage = pausing_sample_service
    uploading = asyncio.create_task(service.upload(harness.identity.user_id, chunks()))
    await asyncio.wait_for(storage.entered.wait(), 5)
    assert len(harness.admission.items) == 1
    uploading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await uploading
    assert not harness.admission.items and not storage.items
    assert all(
        sample.state is SampleState.FAILED and sample.expires_at == harness.clock.now()
        for sample in harness.samples.items.values()
    )


async def test_cleanup_queue_failure_still_expires_state_and_releases_admission(
    harness: SearchHarness,
) -> None:
    repository = CleanupFailureSamples()
    service = SampleService(
        repository=repository,
        admission=harness.admission,
        storage=harness.storage,
        inspector=harness.catalog,
        catalog=harness.catalog,
        clock=harness.clock,
        policy=SearchSessionPolicy(),
        index=harness.index,
        embedding_version=harness.version,
    )
    with pytest.raises(SearchError) as error:
        await service.upload(harness.identity.user_id, chunks())
    assert error.value.code is SearchErrorCode.UNAVAILABLE
    assert not harness.admission.items and not harness.storage.items
    assert all(sample.state is SampleState.FAILED for sample in repository.items.values())


async def test_owner_deletion_clears_admission_and_fences_late_upload(
    harness: SearchHarness,
) -> None:
    await harness.services.samples.upload(harness.identity.user_id, chunks())
    await harness.services.delete_private_data.execute(harness.identity.user_id)
    assert not harness.admission.items
    with pytest.raises(SearchError) as error:
        await harness.services.samples.upload(harness.identity.user_id, chunks())
    assert error.value.code is SearchErrorCode.FORBIDDEN


def test_deployment_rejects_unsupported_physical_dimensions_without_secret(
    service_environment: None,
    configuration_markers: tuple[str, str],
    harness: SearchHarness,
) -> None:
    marker = configuration_markers[1]
    version = replace(
        harness.version,
        dimensions=SearchLimit.MAXIMUM_VECTOR_DIMENSIONS,
        space="jina-v5-omni-nano",
    )
    with pytest.raises(ValidationError) as rejected:
        Settings(
            internal_api_key=marker,
            embedding_dimensions=version.dimensions,
            accepted_embedding_version=version,
        )
    assert "embedding_dimensions" in str(rejected.value)
    assert marker not in str(rejected.value)
