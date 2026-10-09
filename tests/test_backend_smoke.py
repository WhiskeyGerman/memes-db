"""CLI сохраняет только свои fixtures и следует реальным внешним контрактам."""

import argparse
import json
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from PIL import Image

from deploy.scripts.smoke_backend import (
    SmokeConfiguration,
    SmokeState,
    cleanup,
    fixture_png,
    prepare,
    state_path,
    verify,
    write_state,
)


@dataclass
class DisposableBackend:
    owner_id: UUID = field(default_factory=uuid4)
    media_id: UUID = field(default_factory=uuid4)
    upload_id: UUID = field(default_factory=uuid4)
    token: str = field(default_factory=lambda: uuid4().hex + uuid4().hex)
    pending: set[UUID] = field(default_factory=set)
    deleted: bool = False
    quota_rejections: int = 0
    checked_search_links: list[str] = field(default_factory=list)
    registration_login: str | None = None

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "s3.invalid":
            assert "Authorization" not in request.headers
            return httpx.Response(
                HTTPStatus.NO_CONTENT if request.method == "POST" else HTTPStatus.OK
            )
        if path in {"/v1/auth/register", "/v1/auth/login"}:
            payload = json.loads(request.content)
            assert len(payload["login"]) <= 32
            self.registration_login = payload["login"]
            return httpx.Response(
                HTTPStatus.CREATED if path.endswith("register") else HTTPStatus.OK,
                json={"access_token": self.token},
            )
        if path == "/v1/auth/me":
            return (
                httpx.Response(HTTPStatus.UNAUTHORIZED)
                if self.deleted
                else httpx.Response(
                    HTTPStatus.OK,
                    json={"user_id": str(self.owner_id)},
                )
            )
        if path == "/v1/auth/account":
            assert request.method == "DELETE" and json.loads(request.content)["delete_media"]
            self.deleted = True
            return httpx.Response(HTTPStatus.NO_CONTENT)
        if path == "/v1/uploads":
            return httpx.Response(
                HTTPStatus.CREATED,
                json={
                    "upload_id": str(self.upload_id),
                    "upload_url": "http://s3.invalid/upload",
                    "fields": {"key": "own-fixture-key"},
                },
            )
        if path.endswith("/complete"):
            return httpx.Response(HTTPStatus.CREATED, json={"media_id": str(self.media_id)})
        if path.startswith("/v1/media/"):
            return (
                httpx.Response(HTTPStatus.NOT_FOUND)
                if self.deleted
                else httpx.Response(
                    HTTPStatus.OK,
                    json={"media_id": str(self.media_id), "url": "http://s3.invalid/file"},
                )
            )
        if path == "/v1/media":
            return httpx.Response(HTTPStatus.OK, json={"items": [{"media_id": str(self.media_id)}]})
        if path.endswith("/samples"):
            if len(self.pending) >= 2:
                self.quota_rejections += 1
                return httpx.Response(HTTPStatus.TOO_MANY_REQUESTS)
            identifier = uuid4()
            self.pending.add(identifier)
            prefix = path.removesuffix("/samples")
            resource = f"{prefix}/samples/{identifier}"
            return httpx.Response(
                HTTPStatus.ACCEPTED,
                headers={"Location": resource},
                json={
                    "sample_id": str(identifier),
                    "resource_url": resource,
                    "search_url": prefix,
                    "cancel_url": resource,
                    "state": "queued",
                },
            )
        if "/samples/" in path:
            if request.method == "DELETE":
                self.pending.remove(UUID(path.rsplit("/", 1)[-1]))
                return httpx.Response(HTTPStatus.NO_CONTENT)
            return (
                httpx.Response(HTTPStatus.UNAUTHORIZED)
                if self.deleted
                else httpx.Response(
                    HTTPStatus.OK,
                    json={"sample_id": path.rsplit("/", 1)[-1]},
                )
            )
        if request.method == "GET" and "/sessions/" in path:
            return httpx.Response(HTTPStatus.OK, json={})
        if request.method == "POST" and path.startswith("/v1/search/indexes/"):
            self.checked_search_links.append(path)
            return httpx.Response(
                HTTPStatus.OK, json={"resource_url": f"{path}/sessions/{uuid4()}"}
            )
        return httpx.Response(HTTPStatus.NOT_FOUND)


@pytest.fixture
def smoke_configuration(tmp_path: Path) -> SmokeConfiguration:
    return SmokeConfiguration(
        identity_url="http://identity.invalid",
        catalog_url="http://catalog.invalid",
        search_url="http://router.invalid",
        origin="http://frontend.invalid",
        state_path=tmp_path / "closed-state.json",
        release_env=None,
        timeout=1,
    )


@pytest.fixture
def disposable_backend() -> DisposableBackend:
    return DisposableBackend()


def test_prepare_verify_cleanup_checkpoint_uses_only_own_resources(
    smoke_configuration: SmokeConfiguration,
    disposable_backend: DisposableBackend,
) -> None:
    identifier = uuid4()
    state = SmokeState(
        fixture_id=identifier, login=f"smoke_{identifier.hex[:24]}", password=uuid4().hex
    )
    write_state(smoke_configuration.state_path, state, create=True)
    with httpx.Client(transport=httpx.MockTransport(disposable_backend.respond)) as client:
        prepare(client, smoke_configuration, state)
        checkpoint = SmokeState.model_validate_json(smoke_configuration.state_path.read_text())
        assert checkpoint.complete and checkpoint.owner_id == disposable_backend.owner_id
        assert len(checkpoint.samples) == len(disposable_backend.pending) == 2
        assert disposable_backend.quota_rejections == 2
        assert len(checkpoint.query_links) == 2
        verify(client, smoke_configuration, checkpoint)
        assert disposable_backend.checked_search_links[-2:] == [
            sample.search_url for sample in checkpoint.samples
        ]
        cleanup(client, smoke_configuration, checkpoint)
        assert disposable_backend.deleted
        cleanup(client, smoke_configuration, checkpoint)
    with pytest.raises(FileExistsError):
        write_state(smoke_configuration.state_path, state, create=True)


def test_closed_state_path_cannot_escape_ignored_results(tmp_path: Path) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        state_path(str(tmp_path / "secret.json"))


def test_generated_png_is_browser_compatible_and_hash_unique(tmp_path: Path) -> None:
    identifier = uuid4()
    first, second = fixture_png(identifier, "fixture"), fixture_png(identifier, "fixture")
    assert first != second
    path = tmp_path / "fixture.png"
    path.write_bytes(first)
    with Image.open(path) as image:
        assert image.format == "PNG" and image.size == (640, 360)
