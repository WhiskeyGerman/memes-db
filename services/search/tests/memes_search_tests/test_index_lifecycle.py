"""Перестройка, rollback и affinity поверх двух реальных Pg хранилищ."""

import json
from dataclasses import asdict, replace
from http import HTTPStatus
from uuid import uuid4

import httpx
import pytest

from memes_platform.infrastructure.http import InternalHttpClient
from memes_search.application.dto.indexes import IndexTarget
from memes_search.application.dto.projections import (
    CatalogMedia,
    EmbeddingVector,
    FeatureKind,
    FeatureProjection,
    MediaStateChange,
    ProjectionState,
)
from memes_search.application.dto.queries import SearchMode, SearchQuery
from memes_search.application.dto.samples import SampleFeatureChange, SampleState
from memes_search.infrastructure.clients.inference import ProcessingRestGateway
from memes_search.infrastructure.persistence.index_status import PostgreSQLIndexStatus
from memes_search.infrastructure.persistence.sessions import PostgreSQLRequests
from memes_search_tests.deployments import IndexDeployment
from memes_search_tests.fakes import SwitchingSearchProxy
from memes_search_tests.harness import SearchHarness
from memes_search_tests.helpers import chunks

pytestmark = pytest.mark.integration


async def test_parallel_index_generations_retain_rollback_and_share_live_state(
    deployments: tuple[IndexDeployment, IndexDeployment],
    projection: FeatureProjection,
) -> None:
    old, new = deployments
    baseline = replace(
        projection,
        kind=FeatureKind.VISUAL_EMBEDDING,
        text="",
        embedding_version=old.version,
        version_key=json.dumps(asdict(old.version)),
        vectors=(
            EmbeddingVector(values=(1.0, 0.0), space_id=old.version.space, version=old.version),
        ),
    )
    assert await old.services.projections.public_feature(baseline)
    assert not await new.services.projections.public_feature(baseline)
    next_cycle = uuid4()
    pending = replace(
        baseline,
        event_id=uuid4(),
        generation=2,
        cycle_id=next_cycle,
        state=ProjectionState.PENDING,
        embedding_version=new.version,
        version_key=json.dumps(asdict(new.version)),
        vectors=(),
    )
    assert not await old.services.projections.public_feature(pending)
    assert await new.services.projections.public_feature(pending)
    terminal = replace(
        pending,
        event_id=uuid4(),
        state=ProjectionState.AVAILABLE,
        vectors=(
            EmbeddingVector(values=(1.0, 0.0), space_id=new.version.space, version=new.version),
        ),
    )
    assert not await old.services.projections.public_feature(terminal)
    assert await new.services.projections.public_feature(terminal)
    query = SearchQuery(mode=SearchMode.TEXT, text="query")
    for deployment in deployments:
        candidates = await deployment.services.index.vectors(
            query,
            (
                EmbeddingVector(
                    values=(1.0, 0.0), space_id=deployment.version.space, version=deployment.version
                ),
            ),
        )
        assert [item.media_id for item in candidates] == [baseline.media_id]
        coverage = await PostgreSQLIndexStatus(deployment.database).coverage()
        assert coverage.active_media == 1 and coverage.vector_media == 1
        assert coverage.features[0].version_key == json.dumps(asdict(deployment.version))
    moved = MediaStateChange(
        event_id=uuid4(),
        media_id=baseline.media_id,
        revision=2,
        active=True,
        group_id=uuid4(),
        group_revision=2,
        kind=baseline.media_kind,
        published_at=baseline.published_at,
    )
    for deployment in deployments:
        assert await deployment.services.index.apply_media_state(moved)
        result = await deployment.services.index.vectors(
            query,
            (
                EmbeddingVector(
                    values=(1.0, 0.0), space_id=deployment.version.space, version=deployment.version
                ),
            ),
        )
        assert result[0].group_id == moved.group_id
    deleted = MediaStateChange(
        event_id=uuid4(), media_id=baseline.media_id, revision=3, active=False
    )
    for deployment in deployments:
        assert await deployment.services.index.apply_media_state(deleted)
        assert not await deployment.services.index.vectors(
            query,
            (
                EmbeddingVector(
                    values=(1.0, 0.0), space_id=deployment.version.space, version=deployment.version
                ),
            ),
        )
        coverage = await PostgreSQLIndexStatus(deployment.database).coverage()
        assert coverage.active_media == 0 and coverage.tombstones == 1 and not coverage.features


async def test_shadow_private_sample_pins_callback_and_passport_and_owner_erasure_hits_both(
    deployments: tuple[IndexDeployment, IndexDeployment],
    harness: SearchHarness,
) -> None:
    old, new = deployments
    owner = harness.identity.user_id
    primary_sample = await old.services.samples.upload(owner, chunks())
    shadow_sample = await new.services.samples.upload(owner, chunks())
    captured: list[object] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            HTTPStatus.OK, json={"sample_id": str(shadow_sample.id), "status": "pending"}
        )

    client = InternalHttpClient(
        base_url="http://processing.invalid",
        internal_api_key=uuid4().hex,
        transport=httpx.MockTransport(respond),
    )
    gateway = ProcessingRestGateway(
        client,
        wait_seconds=0,
        poll_seconds=0.1,
        dimensions=new.version.dimensions,
        space_id=new.version.space,
        version=new.version,
    )
    try:
        await gateway.submit_sample(shadow_sample)
    finally:
        await client.close()
    assert captured and isinstance(captured[0], dict)
    assert captured[0]["callback_target"] == IndexTarget.SHADOW
    assert captured[0]["feature_version"] == asdict(new.version)
    foreign = SampleFeatureChange(
        event_id=uuid4(),
        sample_id=shadow_sample.id,
        kind=FeatureKind.VISUAL_EMBEDDING,
        cycle_id=uuid4(),
        generation=1,
        state=ProjectionState.AVAILABLE,
        version_key=json.dumps(asdict(old.version)),
        embedding_version=old.version,
        vectors=(
            EmbeddingVector(values=(1.0, 0.0), space_id=old.version.space, version=old.version),
        ),
        sample_state=SampleState.READY,
    )
    assert not await new.services.projections.private_feature(foreign)
    accepted = replace(
        foreign,
        event_id=uuid4(),
        version_key=json.dumps(asdict(new.version)),
        embedding_version=new.version,
        vectors=(
            EmbeddingVector(values=(1.0, 0.0), space_id=new.version.space, version=new.version),
        ),
    )
    assert await new.services.projections.private_feature(accepted)
    for deployment, sample in ((old, primary_sample), (new, shadow_sample)):
        await deployment.services.delete_private_data.execute(owner)
        current = await deployment.services.sample_repository.get(sample.id)
        assert current is not None and current.state is SampleState.CANCELLED
        assert not await deployment.services.projections.private_feature(
            replace(accepted, event_id=uuid4(), sample_id=sample.id)
        )


async def test_public_switch_keeps_old_session_and_request_affinity_links(
    deployments: tuple[IndexDeployment, IndexDeployment],
    projection: FeatureProjection,
    harness: SearchHarness,
) -> None:
    old, new = deployments
    for _ in range(2):
        item = replace(
            projection,
            event_id=uuid4(),
            media_id=uuid4(),
            group_id=uuid4(),
            cycle_id=uuid4(),
            text="cat",
        )
        harness.catalog.items[item.media_id] = CatalogMedia(
            media_id=item.media_id,
            group_id=item.group_id,
            group_revision=1,
            revision=1,
            kind=item.media_kind,
            published_at=item.published_at,
        )
        for deployment in deployments:
            assert await deployment.services.projections.public_feature(item)
    proxy = SwitchingSearchProxy(primary=old.app, shadow=new.app)
    request_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy), base_url="http://edge.invalid"
    ) as client:
        first = await client.post(
            "/v1/search",
            json={"mode": "text", "text": "cat", "page_size": 1, "request_id": str(request_id)},
        )
        assert first.status_code == HTTPStatus.OK
        original = first.json()
        assert original["resource_url"].startswith("/v1/search/indexes/primary/")
        assert first.headers["Location"] == original["resource_url"]
        proxy.current = IndexTarget.SHADOW
        switched = await client.post(
            "/v1/search", json={"mode": "text", "text": "cat", "page_size": 1}
        )
        assert switched.json()["resource_url"].startswith("/v1/search/indexes/shadow/")
        follow = await client.get(original["next_page_url"])
        assert (
            follow.status_code == HTTPStatus.OK
            and follow.json()["session_id"] == original["session_id"]
        )
        resumed = await client.get(f"/v1/search/indexes/primary/requests/{request_id}")
        assert (
            resumed.status_code == HTTPStatus.OK
            and resumed.json()["session_id"] == original["session_id"]
        )
        proxy.current = IndexTarget.PRIMARY
        assert (await client.get(switched.json()["resource_url"])).status_code == HTTPStatus.OK
    assert await PostgreSQLRequests(new.database).get(request_id) is None


async def test_pending_query_and_sample_resources_keep_pinned_target_after_switch(
    deployments: tuple[IndexDeployment, IndexDeployment],
    harness: SearchHarness,
) -> None:
    old, new = deployments
    proxy = SwitchingSearchProxy(primary=old.app, shadow=new.app)
    headers = {"Authorization": f"Bearer {harness.identity.user_id}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy), base_url="http://edge.invalid"
    ) as client:
        pending = await client.post(
            "/v1/search", headers=headers, json={"mode": "semantic", "text": "private request"}
        )
        assert pending.status_code == HTTPStatus.ACCEPTED
        pending_body = pending.json()
        assert pending.headers["Location"] == pending_body["resource_url"]
        primary_upload = await client.post(
            "/v1/search/samples",
            headers=headers,
            files={"file": ("sample.png", b"primary private sample")},
        )
        assert primary_upload.status_code == HTTPStatus.ACCEPTED
        primary_sample = primary_upload.json()
        assert primary_upload.headers["Location"] == primary_sample["resource_url"]
        proxy.current = IndexTarget.SHADOW
        polled = await client.get(pending_body["resource_url"], headers=headers)
        assert polled.status_code == HTTPStatus.ACCEPTED
        assert polled.json()["request_id"] == pending_body["request_id"]
        assert (
            await client.get(primary_sample["resource_url"], headers=headers)
        ).status_code == HTTPStatus.OK
        assert (
            await client.get(primary_sample["download_url"], headers=headers)
        ).status_code == HTTPStatus.OK
        primary_search = await client.post(
            primary_sample["search_url"],
            headers=headers,
            json={"mode": "sample", "sample_id": primary_sample["sample_id"]},
        )
        assert primary_search.status_code == HTTPStatus.ACCEPTED
        assert primary_search.json()["resource_url"].startswith("/v1/search/indexes/primary/")
        shadow_upload = await client.post(
            "/v1/search/samples",
            headers=headers,
            files={"file": ("sample.png", b"shadow private sample")},
        )
        assert shadow_upload.status_code == HTTPStatus.ACCEPTED
        shadow_sample = shadow_upload.json()
        assert shadow_sample["resource_url"].startswith("/v1/search/indexes/shadow/")
        proxy.current = IndexTarget.PRIMARY
        assert (
            await client.get(shadow_sample["resource_url"], headers=headers)
        ).status_code == HTTPStatus.OK
        shadow_search = await client.post(
            shadow_sample["search_url"],
            headers=headers,
            json={"mode": "sample", "sample_id": shadow_sample["sample_id"]},
        )
        assert shadow_search.status_code == HTTPStatus.ACCEPTED
        assert shadow_search.json()["resource_url"].startswith("/v1/search/indexes/shadow/")
        deleted = await client.delete(primary_sample["cancel_url"], headers=headers)
        assert deleted.status_code == HTTPStatus.NO_CONTENT
        assert (
            await client.get(primary_sample["resource_url"], headers=headers)
        ).status_code == HTTPStatus.NOT_FOUND
        assert (
            await client.get(shadow_sample["resource_url"], headers=headers)
        ).status_code == HTTPStatus.OK


async def test_primary_and_shadow_http_share_quota_and_cancel_returns_global_slot(
    deployments: tuple[IndexDeployment, IndexDeployment],
    harness: SearchHarness,
) -> None:
    old, new = deployments
    proxy = SwitchingSearchProxy(primary=old.app, shadow=new.app)
    headers = {"Authorization": f"Bearer {harness.identity.user_id}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy), base_url="http://edge.invalid"
    ) as client:
        first = await client.post(
            "/v1/search/indexes/primary/samples",
            headers=headers,
            files={"file": ("first.png", b"primary pending")},
        )
        second = await client.post(
            "/v1/search/indexes/shadow/samples",
            headers=headers,
            files={"file": ("second.png", b"shadow pending")},
        )
        assert first.status_code == second.status_code == HTTPStatus.ACCEPTED
        for target in IndexTarget:
            third = await client.post(
                f"/v1/search/indexes/{target}/samples",
                headers=headers,
                files={"file": ("third.png", b"third pending")},
            )
            assert third.status_code == HTTPStatus.TOO_MANY_REQUESTS
        assert (await client.delete(first.json()["cancel_url"], headers=headers)).status_code == (
            HTTPStatus.NO_CONTENT
        )
        replacement = await client.post(
            "/v1/search/indexes/shadow/samples",
            headers=headers,
            files={"file": ("replacement.png", b"replacement pending")},
        )
        assert replacement.status_code == HTTPStatus.ACCEPTED
