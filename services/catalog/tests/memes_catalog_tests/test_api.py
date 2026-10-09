"""HTTP-граница Catalog с настоящими публикациями, PostgreSQL и S3."""

import asyncio
from collections.abc import Callable
from hashlib import sha256
from http import HTTPStatus
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from memes_catalog.application.dto.catalog import Actor
from memes_catalog.infrastructure.persistence.models import MediaRow
from memes_catalog_tests.conftest import CatalogHarness

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("authorization", [None, "Basic invalid", "Bearer invalid"])
async def test_mutation_requires_identity_token(
    client: httpx.AsyncClient, authorization: str | None
) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}
    response = await client.post("/v1/uploads", json={"size_bytes": 100}, headers=headers)
    assert response.status_code == HTTPStatus.UNAUTHORIZED


async def test_internal_projection_requires_service_key(
    client: httpx.AsyncClient, harness: CatalogHarness
) -> None:
    response = await client.post("/internal/media/resolve", json={"media_ids": []})
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    valid = await client.post(
        "/internal/media/resolve",
        json={"media_ids": []},
        headers={"X-Internal-Key": harness.settings.internal_api_key},
    )
    assert valid.status_code == HTTPStatus.OK
    assert valid.json() == {"items": [], "missing": []}


async def test_public_view_hides_deleted_login_and_delete_checks_owner(
    client: httpx.AsyncClient,
    harness: CatalogHarness,
    actor: Actor,
    png: Callable[[int], bytes],
) -> None:
    view = await harness.publish(actor, png(25))
    denied = await client.delete(
        f"/v1/media/{view.media.id}",
        headers={"Authorization": f"Bearer {uuid4()}"},
    )
    assert denied.status_code == HTTPStatus.FORBIDDEN
    harness.identity.deleted[actor.user_id] = False
    public = await client.get(f"/v1/media/{view.media.id}")
    assert public.status_code == HTTPStatus.OK
    assert public.json()["author"]["login"] is None
    assert public.json()["author"]["display_name"] == "удаленный аккаунт"
    assert public.json()["membership_id"] == str(view.membership.id)


async def test_private_sample_inspection_never_publishes(
    client: httpx.AsyncClient,
    harness: CatalogHarness,
    png: Callable[[int], bytes],
) -> None:
    sample_id, file_id = uuid4(), uuid4()
    key = f"samples/{sample_id}/{file_id}"
    body = png(26)
    try:
        await asyncio.to_thread(
            harness.storage.client.put_object,
            Bucket=harness.storage.bucket,
            Key=key,
            Body=body,
        )
        data = {
            "object_key": key,
            "file_id": str(file_id),
            "sha256": sha256(body).hexdigest(),
            "size_bytes": len(body),
        }
        headers = {"X-Internal-Key": harness.settings.internal_api_key}
        inspected = await client.post("/internal/media/inspect", json=data, headers=headers)
        assert inspected.status_code == HTTPStatus.OK
        assert inspected.json()["kind"] == "image"
        assert inspected.json()["sha256"] == data["sha256"]
        mismatched = await client.post(
            "/internal/media/inspect",
            json={**data, "sha256": "0" * 64},
            headers=headers,
        )
        assert mismatched.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        async with harness.sessions() as session:
            assert not (await session.scalars(select(MediaRow))).all()
    finally:
        await harness.storage.delete(key)


async def test_presigned_browser_upload_and_exact_duplicate_return_existing_link(
    client: httpx.AsyncClient,
    harness: CatalogHarness,
    actor: Actor,
    png: Callable[[int], bytes],
) -> None:
    body = png(27)
    headers = {"Authorization": f"Bearer {actor.user_id}"}
    outcomes: list[httpx.Response] = []
    for _ in range(2):
        created = await client.post("/v1/uploads", json={"size_bytes": len(body)}, headers=headers)
        assert created.status_code == HTTPStatus.CREATED
        ticket = created.json()
        async with httpx.AsyncClient() as transfer:
            uploaded = await transfer.post(
                ticket["upload_url"],
                data=ticket["fields"],
                files={"file": ("sample.png", body, "image/png")},
            )
        assert uploaded.is_success
        outcomes.append(
            await client.post(f"/v1/uploads/{ticket['upload_id']}/complete", headers=headers)
        )
    assert outcomes[0].status_code == HTTPStatus.CREATED
    assert outcomes[1].status_code == HTTPStatus.CONFLICT
    assert outcomes[1].json()["media_id"] == outcomes[0].json()["media_id"]
    assert outcomes[1].json()["url"] == f"/v1/media/{outcomes[0].json()['media_id']}"
