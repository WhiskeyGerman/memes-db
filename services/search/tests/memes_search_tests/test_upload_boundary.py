"""Отказ авторизации и квоты происходит до чтения multipart байтов."""

from http import HTTPStatus

import httpx
import pytest
from starlette.requests import Request
from starlette.types import Message, Scope

from memes_search.application.dto.samples import SampleState
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.presentation.sample_upload import sample_chunks
from memes_search_tests.fakes import MultipartSpools, ReceiveSpy
from memes_search_tests.harness import SearchHarness
from memes_search_tests.helpers import chunks


async def test_unauthenticated_upload_does_not_read_any_body(
    spy_client: httpx.AsyncClient,
    receive_spy: ReceiveSpy,
    harness: SearchHarness,
) -> None:
    response = await spy_client.post(
        "/v1/search/samples", files={"file": ("sample.png", b"private content")}
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert receive_spy.calls == 0
    assert not harness.samples.items and not harness.storage.items


async def test_exhausted_quota_does_not_parse_or_spool_another_file(
    spy_client: httpx.AsyncClient,
    receive_spy: ReceiveSpy,
    harness: SearchHarness,
) -> None:
    owner = harness.identity.user_id
    await harness.services.samples.upload(owner, chunks())
    await harness.services.samples.upload(owner, chunks())
    response = await spy_client.post(
        "/v1/search/samples",
        headers={"Authorization": f"Bearer {owner}"},
        files={"file": ("sample.png", b"private content")},
    )
    assert response.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert receive_spy.calls == 0 and len(harness.samples.items) == 2


async def test_authorized_upload_reads_form_after_reservation(
    spy_client: httpx.AsyncClient,
    receive_spy: ReceiveSpy,
    harness: SearchHarness,
) -> None:
    owner = harness.identity.user_id
    response = await spy_client.post(
        "/v1/search/samples",
        headers={"Authorization": f"Bearer {owner}"},
        files={"file": ("sample.png", b"private content")},
    )
    assert response.status_code == HTTPStatus.ACCEPTED
    assert receive_spy.calls > 0 and len(harness.samples.items) == 1
    assert response.json()["state"] == SampleState.QUEUED
    assert tuple(harness.storage.items.values()) == (b"private content",)


@pytest.mark.parametrize("form", ["wrong_file_field", "missing_boundary"])
async def test_invalid_form_expires_reservation_and_drops_private_content(
    spy_client: httpx.AsyncClient,
    harness: SearchHarness,
    form: str,
) -> None:
    headers = {"Authorization": f"Bearer {harness.identity.user_id}"}
    if form == "wrong_file_field":
        response = await spy_client.post(
            "/v1/search/samples",
            headers=headers,
            files={"wrong": ("sample.png", b"private content")},
        )
    else:
        headers["Content-Type"] = "multipart/form-data"
        response = await spy_client.post(
            "/v1/search/samples", headers=headers, content=b"invalid form"
        )
    assert response.status_code == HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    assert len(harness.samples.items) == 1 and not harness.storage.items
    sample = next(iter(harness.samples.items.values()))
    assert sample.state is SampleState.FAILED and sample.expires_at == harness.clock.now()


async def test_public_search_rejects_large_json_before_receive(
    spy_client: httpx.AsyncClient,
    receive_spy: ReceiveSpy,
) -> None:
    response = await spy_client.post(
        "/v1/search", headers={"Content-Length": str(1_048_576)}, content=b"oversized public body"
    )
    assert response.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert receive_spy.calls == 0


@pytest.mark.parametrize("disconnect", [True, False])
async def test_unfinished_form_closes_every_created_scratch_file(
    multipart_spools: MultipartSpools,
    disconnect: bool,
) -> None:
    boundary = "private-multipart-test"
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="sample.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode() + b"private partial content"
    calls = 0

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": prefix, "more_body": disconnect}
        return {"type": "http.disconnect"}

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/search/samples",
        "headers": [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
    }
    chunks = sample_chunks(Request(scope, receive=receive))
    try:
        with pytest.raises(SearchError) as error:
            async for _ in chunks:
                pass
        assert error.value.code is SearchErrorCode.INVALID_SAMPLE
    finally:
        await chunks.aclose()
    assert multipart_spools.files and all(file.closed for file in multipart_spools.files)
