from http import HTTPStatus
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from memes_search.application.dto.indexes import IndexTarget
from memes_search.presentation.links import SearchResourceLinks

pytest_plugins = ["memes_search_tests.index_alias_fixtures"]


@pytest.mark.parametrize("alias_probe", list(IndexTarget), indirect=True)
async def test_alias_preserves_suffix_raw_encoding_and_query(alias_probe: FastAPI) -> None:
    prefix = alias_probe.state.runtime.services.links.prefix
    suffix = "/sessions/%D0%BA%D0%BE%D1%82"
    query = "offset=1&limit=2&opaque=%2F%2B"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=alias_probe), base_url="http://search.invalid"
    ) as client:
        response = await client.get(f"{prefix}{suffix}?{query}")
        assert response.json() == {
            "path": "/v1/search/sessions/кот",
            "raw_path": f"/v1/search{suffix}",
            "query_string": query,
        }
        bare = await client.post(prefix, json={"sample_id": str(uuid4())})
        assert bare.json()["path"] == "/v1/search"
        assert bare.json()["raw_path"] == "/v1/search"
        escaped = await client.get(prefix.replace("/v1", "/%76%31") + suffix)
        assert escaped.json()["raw_path"] == f"/v1/search{suffix}"


@pytest.mark.parametrize("alias_probe", list(IndexTarget), indirect=True)
async def test_alias_leaves_other_index_generic_and_internal_paths_unchanged(
    alias_probe: FastAPI,
) -> None:
    own_prefix = alias_probe.state.runtime.services.links.prefix
    other = IndexTarget.SHADOW if own_prefix.endswith(IndexTarget.PRIMARY) else IndexTarget.PRIMARY
    paths = [
        SearchResourceLinks.for_target(other).prefix,
        own_prefix + "-unrelated",
        "/v1/search/samples",
        "/internal/search/features",
        "/health/live",
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=alias_probe), base_url="http://search.invalid"
    ) as client:
        for path in paths:
            response = await client.get(path + "?opaque=%2B")
            assert response.json() == {
                "path": path,
                "raw_path": path,
                "query_string": "opaque=%2B",
            }


@pytest.mark.parametrize("alias_probe", [IndexTarget.PRIMARY], indirect=True)
async def test_uninitialized_test_app_uses_primary_alias(alias_probe: FastAPI) -> None:
    del alias_probe.state.runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=alias_probe), base_url="http://search.invalid"
    ) as client:
        response = await client.post("/v1/search/indexes/primary")
    assert response.json()["path"] == "/v1/search"


@pytest.mark.parametrize("standalone_index_app", list(IndexTarget), indirect=True)
async def test_real_app_serves_own_alias_and_preserves_upload_body_limit(
    standalone_index_app: FastAPI,
) -> None:
    prefix = standalone_index_app.state.runtime.services.links.prefix
    other = IndexTarget.SHADOW if prefix.endswith(IndexTarget.PRIMARY) else IndexTarget.PRIMARY
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=standalone_index_app),
        base_url="http://search.invalid",
    ) as client:
        unauthorized = await client.get(f"{prefix}/samples/{uuid4()}")
        assert unauthorized.status_code == HTTPStatus.UNAUTHORIZED
        upload = await client.post(
            f"{prefix}/samples",
            headers={"Content-Length": str(300_000)},
        )
        assert upload.status_code == HTTPStatus.UNAUTHORIZED
        missing = await client.get(
            f"{SearchResourceLinks.for_target(other).prefix}/samples/{uuid4()}"
        )
        assert missing.status_code == HTTPStatus.NOT_FOUND
