"""Проверки точных образцов, транспорта и независимых признаков."""

import json
from dataclasses import replace
from http import HTTPStatus
from types import SimpleNamespace
from uuid import uuid4

import httpx
from fastapi import FastAPI

from memes_search.application.dto.projections import EmbeddingVector, EmbeddingVersion
from memes_search.application.dto.samples import SampleFeatures, SampleState
from memes_search.application.use_cases.projections import ProjectionService
from memes_search_tests.harness import SearchHarness
from memes_search_tests.helpers import chunks, populate


async def test_exact_sample_reuses_features_without_ml_queue(harness: SearchHarness) -> None:
    source, _ = populate(harness)
    harness.catalog.exact = source
    harness.index.reusable[source.media_id] = SampleFeatures(
        vectors=(
            EmbeddingVector(values=(1.0, 0.0), space_id="fixture-space", version=harness.version),
        )
    )
    sample = await harness.services.samples.upload(harness.identity.user_id, chunks())
    assert sample.state is SampleState.READY
    assert sample.exact_media_id == source.media_id
    assert not harness.samples.queued


async def test_excess_content_length_is_rejected_before_multipart(
    client: httpx.AsyncClient,
    harness: SearchHarness,
) -> None:
    response = await client.post("/v1/search/samples", headers={"Content-Length": str(1 << 40)})
    assert response.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert not harness.samples.items


async def test_owner_deletion_revokes_samples_and_retains_other_owner(
    harness: SearchHarness,
) -> None:
    owner, other = uuid4(), uuid4()
    sample = await harness.services.samples.upload(owner, chunks())
    retained = await harness.services.samples.upload(other, chunks())
    await harness.services.delete_private_data.execute(owner)
    assert harness.samples.items[sample.id].state is SampleState.CANCELLED
    assert harness.samples.items[sample.id].expires_at == harness.clock.now()
    assert harness.samples.items[retained.id].state is SampleState.QUEUED


async def test_complete_animation_embedding_delivery_exceeds_small_json_cap(
    client: httpx.AsyncClient, app: FastAPI, harness: SearchHarness
) -> None:
    key = uuid4().hex
    app.state.settings = SimpleNamespace(internal_api_key=key)
    values = [0.12345678901234568] * 2048
    body = {
        "event_id": str(uuid4()),
        "media_id": str(uuid4()),
        "group_id": str(uuid4()),
        "group_revision": 1,
        "revision": 1,
        "published_at": "2026-10-01T00:00:00Z",
        "media_kind": "animation",
        "kind": "visual_embedding",
        "cycle_id": str(uuid4()),
        "generation": 1,
        "state": "available",
        "feature_version": {
            "model": "fixture-model",
            "weights": "fixture-weights",
            "task_adapter": "vision",
            "preprocessing": "fixture-2fps-600",
            "space": "fixture-space",
            "dimensions": 2048,
        },
        "result": {
            "items": [
                {"values": values, "span": {"start_ms": index * 500, "end_ms": (index + 1) * 500}}
                for index in range(600)
            ]
        },
    }
    passport = EmbeddingVersion(
        model="fixture-model",
        weights="fixture-weights",
        task_adapter="vision",
        preprocessing="fixture-2fps-600",
        space="fixture-space",
        dimensions=2048,
    )
    harness.services = replace(
        harness.services,
        projections=ProjectionService(
            index=harness.index,
            samples=harness.samples,
            admission=harness.admission,
            clock=harness.clock,
            accepted_version=passport,
        ),
    )
    encoded = json.dumps(body).encode()
    assert len(encoded) > 16_777_216
    response = await client.post(
        "/internal/search/features",
        content=encoded,
        headers={"X-Internal-Key": key, "Content-Type": "application/json"},
    )
    assert response.status_code == HTTPStatus.OK and response.json()["applied"]


async def test_extended_feature_cap_does_not_expand_public_or_untrusted_requests(
    client: httpx.AsyncClient, app: FastAPI, harness: SearchHarness
) -> None:
    key = uuid4().hex
    app.state.settings = SimpleNamespace(internal_api_key=key)
    for path, provided in (
        ("/v1/search", key),
        ("/internal/search/features", uuid4().hex),
        ("/internal/search/features/unknown", key),
    ):
        response = await client.post(
            path,
            headers={"X-Internal-Key": provided, "Content-Length": str(32 * 1024 * 1024)},
        )
        assert response.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
