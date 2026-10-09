"""Контракт публичной авторизации и явных статусов Search."""

from datetime import UTC, datetime
from http import HTTPStatus
from types import SimpleNamespace
from uuid import uuid4

import httpx
from fastapi import FastAPI

from memes_search.application.dto.queries import SearchMode, SearchQuery
from memes_search.application.dto.samples import InferenceState
from memes_search_tests.harness import SearchHarness
from memes_search_tests.helpers import populate


async def test_liveness_is_independent_of_configuration(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "alive"}


async def test_pending_semantic_request_returns_accepted(
    client: httpx.AsyncClient,
    harness: SearchHarness,
) -> None:
    harness.inference.state = InferenceState.PENDING
    response = await client.post("/v1/search", json={"mode": "semantic", "text": "cat"})
    assert response.status_code == HTTPStatus.ACCEPTED
    assert response.json()["request_id"]


async def test_hybrid_failure_is_publicly_explained(
    client: httpx.AsyncClient,
    harness: SearchHarness,
) -> None:
    populate(harness)
    harness.inference.failure = True
    response = await client.post("/v1/search", json={"text": "cat"})
    assert response.status_code == HTTPStatus.OK
    assert response.json()["limitation"] == "semantic_search_unavailable"


async def test_upload_requires_registered_account(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/search/samples", files={"file": ("sample.png", b"bytes")})
    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_invalid_bearer_is_not_treated_as_guest(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/search",
        json={"mode": "text", "text": "cat"},
        headers={"Authorization": "Bearer invalid"},
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_internal_user_delete_requires_service_key(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    app.state.settings = SimpleNamespace(internal_api_key=uuid4().hex)
    response = await client.post(
        f"/internal/search/users/{uuid4()}/delete",
        json={"event_id": str(uuid4()), "occurred_at": datetime.now(UTC).isoformat()},
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_internal_user_delete_with_key_erases_only_owner(
    client: httpx.AsyncClient,
    app: FastAPI,
    harness: SearchHarness,
) -> None:
    key = uuid4().hex
    app.state.settings = SimpleNamespace(internal_api_key=key)
    owner, other = uuid4(), uuid4()
    await harness.services.search.execute(
        SearchQuery(mode=SearchMode.TEXT, text="private query", owner_id=owner), uuid4()
    )
    await harness.services.search.execute(
        SearchQuery(mode=SearchMode.TEXT, text="other private query", owner_id=other), uuid4()
    )
    response = await client.post(
        f"/internal/search/users/{owner}/delete",
        headers={"X-Internal-Key": key},
        json={"event_id": str(uuid4()), "occurred_at": datetime.now(UTC).isoformat()},
    )
    assert response.status_code == HTTPStatus.OK
    assert all(item.query.owner_id == other for item in harness.sessions.items.values())
    assert all(item.query.owner_id == other for item in harness.requests.items.values())
