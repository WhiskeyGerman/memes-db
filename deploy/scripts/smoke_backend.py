"""Disposable HTTP smoke с сохранением affinity до cutover и удалением своих данных."""

import argparse
import asyncio
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from http import HTTPStatus
from io import BytesIO
from os import O_CREAT, O_EXCL, O_WRONLY, fdopen, replace
from os import open as os_open
from pathlib import Path
from secrets import token_urlsafe
from stat import S_IRUSR, S_IWUSR
from sys import stdout
from time import monotonic, sleep
from uuid import UUID, uuid4

import httpx
from PIL import Image, ImageDraw, PngImagePlugin
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine


class SmokeMode(StrEnum):
    """Последовательные фазы одного disposable сценария."""

    PREPARE = "prepare"
    VERIFY = "verify"
    CLEANUP = "cleanup"


class SmokeIndex(StrEnum):
    """Устойчивые адреса двух физических индексов."""

    PRIMARY = "primary"
    SHADOW = "shadow"


class SmokeLimit(IntEnum):
    """Конечные технические границы smoke, а не runtime политики продукта."""

    HTTP_SECONDS = 15
    WAIT_SECONDS = 180
    POLL_SECONDS = 1
    PASSWORD_BYTES = 32
    LOGIN_SUFFIX = 24
    TEXT_LEFT = 20
    TEXT_TOP = 40
    IMAGE_WIDTH = 640
    IMAGE_HEIGHT = 360
    POSTGRES_PORT = 55433


class SmokeError(RuntimeError):
    """Безопасный машинный код отказа без HTTP body, URL или credentials."""


class SampleLink(BaseModel):
    """Минимальный внешний ресурс приватного образца."""

    sample_id: UUID
    resource_url: str
    search_url: str
    cancel_url: str
    state: str


class SmokeState(BaseModel):
    """Закрытый checkpoint только объектов, созданных этим запуском."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    fixture_id: UUID
    login: str = Field(repr=False)
    password: str = Field(repr=False)
    access_token: str | None = Field(default=None, repr=False)
    owner_id: UUID | None = None
    media_ids: list[UUID] = Field(default_factory=list)
    samples: list[SampleLink] = Field(default_factory=list)
    query_links: list[str] = Field(default_factory=list)
    deleted: bool = False
    complete: bool = False


@dataclass(frozen=True, slots=True)
class SmokeConfiguration:
    """Адреса явных backing resources и путь закрытого checkpoint."""

    identity_url: str
    catalog_url: str
    search_url: str
    origin: str
    state_path: Path
    release_env: Path | None
    timeout: int


def state_path(value: str) -> Path:
    """Разрешить credential checkpoint только внутри ignored deploy/results."""
    results = Path(__file__).resolve().parents[1] / "results"
    resolved = Path(value).resolve()
    if not resolved.is_relative_to(results) or resolved == results:
        raise argparse.ArgumentTypeError("state_must_be_inside_deploy_results")
    return resolved


def write_state(path: Path, state: SmokeState, *, create: bool = False) -> None:
    """Записать первый checkpoint эксклюзивно, последующие заменить атомарно."""
    path.parent.mkdir(parents=True, exist_ok=True)
    destination = path if create else path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with fdopen(
            os_open(destination, O_WRONLY | O_CREAT | O_EXCL, S_IRUSR | S_IWUSR),
            "w",
            encoding="utf-8",
            newline="\n",
        ) as output:
            output.write(state.model_dump_json(indent=2))
        if not create:
            replace(destination, path)
    finally:
        if not create:
            destination.unlink(missing_ok=True)


def expect(response: httpx.Response, *statuses: HTTPStatus) -> httpx.Response:
    """Проверить HTTP-контракт, не раскрывая body в исключении."""
    if response.status_code not in statuses:
        raise SmokeError(f"unexpected_http_status_{response.status_code}")
    return response


def object_body(response: httpx.Response) -> dict[str, object]:
    """Декодировать ожидаемый JSON object без неявного Any."""
    body: object = response.json()
    if not isinstance(body, dict) or not all(isinstance(key, str) for key in body):
        raise SmokeError("invalid_json_object")
    return body


def string_field(body: dict[str, object], key: str) -> str:
    """Извлечь обязательное строковое поле внешнего контракта."""
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise SmokeError("invalid_string_field")
    return value


def bearer(state: SmokeState) -> dict[str, str]:
    """Предъявить secret только API, никогда signed S3 endpoint."""
    if state.access_token is None:
        raise SmokeError("account_not_authenticated")
    return {"Authorization": f"Bearer {state.access_token}"}


def authenticate(
    client: httpx.Client,
    configuration: SmokeConfiguration,
    state: SmokeState,
    *,
    register: bool,
) -> None:
    """Зарегистрировать disposable account либо обновить access через login."""
    endpoint = "/v1/auth/register" if register else "/v1/auth/login"
    response = expect(
        client.post(
            configuration.identity_url + endpoint,
            headers={"Origin": configuration.origin},
            json={"login": state.login, "password": state.password},
        ),
        HTTPStatus.CREATED if register else HTTPStatus.OK,
    )
    state.access_token = string_field(object_body(response), "access_token")
    me = object_body(
        expect(
            client.get(
                configuration.identity_url + "/v1/auth/me",
                headers=bearer(state),
            ),
            HTTPStatus.OK,
        )
    )
    owner_id = UUID(string_field(me, "user_id"))
    if state.owner_id is not None and state.owner_id != owner_id:
        raise SmokeError("account_identity_changed")
    state.owner_id = owner_id
    write_state(configuration.state_path, state)


def fixture_png(fixture_id: UUID, label: str) -> bytes:
    """Создать браузерный PNG с уникальными байтами без сторонних изображений."""
    image = Image.new("RGB", (SmokeLimit.IMAGE_WIDTH, SmokeLimit.IMAGE_HEIGHT), "white")
    ImageDraw.Draw(image).text(
        (SmokeLimit.TEXT_LEFT, SmokeLimit.TEXT_TOP), f"BACKEND SMOKE CAT {label}", fill="black"
    )
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("fixture", f"{fixture_id}/{label}/{uuid4()}")
    with BytesIO() as output:
        image.save(output, format="PNG", pnginfo=metadata)
        return output.getvalue()


def upload_media(
    client: httpx.Client,
    configuration: SmokeConfiguration,
    state: SmokeState,
) -> None:
    """Пройти настоящий signed POST S3 и немедленную публикацию Catalog."""
    data = fixture_png(state.fixture_id, "public")
    ticket = object_body(
        expect(
            client.post(
                configuration.catalog_url + "/v1/uploads",
                headers=bearer(state),
                json={"size_bytes": len(data)},
            ),
            HTTPStatus.CREATED,
        )
    )
    fields = ticket.get("fields")
    if not isinstance(fields, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in fields.items()
    ):
        raise SmokeError("invalid_s3_upload_fields")
    expect(
        client.post(
            string_field(ticket, "upload_url"),
            data=fields,
            files={"file": ("fixture.png", data, "image/png")},
        ),
        HTTPStatus.NO_CONTENT,
        HTTPStatus.CREATED,
        HTTPStatus.OK,
    )
    completed = object_body(
        expect(
            client.post(
                configuration.catalog_url
                + f"/v1/uploads/{string_field(ticket, 'upload_id')}/complete",
                headers=bearer(state),
            ),
            HTTPStatus.CREATED,
        )
    )
    identifier = UUID(string_field(completed, "media_id"))
    state.media_ids.append(identifier)
    write_state(configuration.state_path, state)
    published = object_body(
        expect(
            client.get(
                configuration.catalog_url + f"/v1/media/{identifier}",
            ),
            HTTPStatus.OK,
        )
    )
    if string_field(published, "media_id") != str(identifier):
        raise SmokeError("published_media_identity_mismatch")
    expect(client.get(string_field(published, "url")), HTTPStatus.OK)
    feed = object_body(expect(client.get(configuration.catalog_url + "/v1/media"), HTTPStatus.OK))
    items = feed.get("items")
    if not isinstance(items, list) or not any(
        isinstance(item, dict) and item.get("media_id") == str(identifier) for item in items
    ):
        raise SmokeError("immediate_publication_missing_from_feed")


def upload_sample(
    client: httpx.Client,
    configuration: SmokeConfiguration,
    state: SmokeState,
    target: SmokeIndex,
) -> SampleLink:
    """Создать один private ресурс в заданном index, сохранив stable links."""
    response = expect(
        client.post(
            f"{configuration.search_url}/v1/search/indexes/{target}/samples",
            headers=bearer(state),
            files={
                "file": (
                    "fixture.png",
                    fixture_png(state.fixture_id, target),
                    "image/png",
                )
            },
        ),
        HTTPStatus.ACCEPTED,
    )
    sample = SampleLink.model_validate(object_body(response))
    if not sample.resource_url.startswith(f"/v1/search/indexes/{target}/"):
        raise SmokeError("sample_affinity_missing")
    if response.headers.get("Location") != sample.resource_url:
        raise SmokeError("sample_location_mismatch")
    state.samples.append(sample)
    write_state(configuration.state_path, state)
    return sample


def prepare(
    client: httpx.Client,
    configuration: SmokeConfiguration,
    state: SmokeState,
) -> None:
    """Создать disposable fixtures; pending quota требует остановленных ML workers."""
    authenticate(client, configuration, state, register=True)
    upload_media(client, configuration, state)
    first = upload_sample(client, configuration, state, SmokeIndex.PRIMARY)
    upload_sample(client, configuration, state, SmokeIndex.SHADOW)
    for target in SmokeIndex:
        expect(
            client.post(
                f"{configuration.search_url}/v1/search/indexes/{target}/samples",
                headers=bearer(state),
                files={
                    "file": (
                        "rejected.png",
                        fixture_png(state.fixture_id, "rejected"),
                        "image/png",
                    )
                },
            ),
            HTTPStatus.TOO_MANY_REQUESTS,
        )
    expect(
        client.delete(
            configuration.search_url + first.cancel_url,
            headers=bearer(state),
        ),
        HTTPStatus.NO_CONTENT,
    )
    state.samples.remove(first)
    write_state(configuration.state_path, state)
    upload_sample(client, configuration, state, SmokeIndex.PRIMARY)
    for target in SmokeIndex:
        result = object_body(
            expect(
                client.post(
                    f"{configuration.search_url}/v1/search/indexes/{target}",
                    headers=bearer(state),
                    json={"mode": "text", "text": str(state.fixture_id), "page_size": 1},
                ),
                HTTPStatus.OK,
            )
        )
        link = string_field(result, "resource_url")
        if not link.startswith(f"/v1/search/indexes/{target}/"):
            raise SmokeError("query_affinity_missing")
        state.query_links.append(link)
        write_state(configuration.state_path, state)
    state.complete = True
    write_state(configuration.state_path, state)


def verify(client: httpx.Client, configuration: SmokeConfiguration, state: SmokeState) -> None:
    """Повторить сохранённые ссылки после cutover в пределах server TTL."""
    if not state.complete or state.deleted:
        raise SmokeError("prepare_incomplete_or_already_deleted")
    authenticate(client, configuration, state, register=False)
    for link in state.query_links:
        expect(client.get(configuration.search_url + link, headers=bearer(state)), HTTPStatus.OK)
    for sample in state.samples:
        current = object_body(
            expect(
                client.get(
                    configuration.search_url + sample.resource_url,
                    headers=bearer(state),
                ),
                HTTPStatus.OK,
            )
        )
        if string_field(current, "sample_id") != str(sample.sample_id):
            raise SmokeError("sample_identity_mismatch_after_cutover")
        response = expect(
            client.post(
                configuration.search_url + sample.search_url,
                headers=bearer(state),
                json={"mode": "sample", "sample_id": str(sample.sample_id)},
            ),
            HTTPStatus.OK,
            HTTPStatus.ACCEPTED,
        )
        prefix = sample.search_url + "/"
        if not string_field(object_body(response), "resource_url").startswith(prefix):
            raise SmokeError("sample_post_affinity_mismatch_after_cutover")


def release_settings(path: Path) -> dict[str, str]:
    """Прочитать ignored release env, не выводя его содержимое."""
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )


async def private_data_erased(configuration: SmokeConfiguration, state: SmokeState) -> bool:
    """Проверить только owner UUID этого smoke в обеих физических DB."""
    if configuration.release_env is None:
        return True
    settings = release_settings(configuration.release_env)
    for key in ("MEMES_SEARCH_DATABASE_URL", "MEMES_SEARCH_SHADOW_DATABASE_URL"):
        url = make_url(settings[key]).set(
            host="127.0.0.1",
            port=int(settings.get("MEMES_POSTGRES_PORT", SmokeLimit.POSTGRES_PORT)),
        )
        engine = create_async_engine(url, hide_parameters=True)
        try:
            async with engine.connect() as connection:
                values = {"owner": state.owner_id, "owner_text": str(state.owner_id)}
                fenced = await connection.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM search_deleted_owners WHERE owner_id=:owner)"
                    ),
                    values,
                )
                remaining = await connection.scalar(
                    text(
                        "SELECT (SELECT count(*) FROM search_samples WHERE owner_id=:owner) "
                        "+ (SELECT count(*) FROM search_sample_admissions WHERE owner_id=:owner) "
                        "+ (SELECT count(*) FROM search_sessions "
                        "WHERE body->'query'->>'owner_id'=:owner_text) "
                        "+ (SELECT count(*) FROM search_requests "
                        "WHERE body->'query'->>'owner_id'=:owner_text)"
                    ),
                    values,
                )
                if not fenced or remaining:
                    return False
        finally:
            await engine.dispose()
    return True


def cleanup(client: httpx.Client, configuration: SmokeConfiguration, state: SmokeState) -> None:
    """Удалить account/media этого запуска и дождаться обоих owner fences."""
    if not state.deleted:
        authenticate(client, configuration, state, register=False)
        expect(
            client.request(
                "DELETE",
                configuration.identity_url + "/v1/auth/account",
                headers=bearer(state),
                json={"password": state.password, "delete_media": True},
            ),
            HTTPStatus.NO_CONTENT,
        )
        state.deleted = True
        write_state(configuration.state_path, state)
    expect(
        client.get(
            configuration.identity_url + "/v1/auth/me",
            headers=bearer(state),
        ),
        HTTPStatus.UNAUTHORIZED,
    )
    for sample in state.samples:
        expect(
            client.get(
                configuration.search_url + sample.resource_url,
                headers=bearer(state),
            ),
            HTTPStatus.UNAUTHORIZED,
        )
    deadline = monotonic() + configuration.timeout
    while monotonic() < deadline:
        media_gone = all(
            client.get(
                configuration.catalog_url + f"/v1/media/{identifier}",
            ).status_code
            == HTTPStatus.NOT_FOUND
            for identifier in state.media_ids
        )
        if media_gone and asyncio.run(private_data_erased(configuration, state)):
            write_state(configuration.state_path, state)
            return
        sleep(SmokeLimit.POLL_SECONDS)
    raise SmokeError("own_fixture_cleanup_deadline_exceeded")


def main() -> None:
    """Исполнить одну воспроизводимую фазу без вывода private checkpoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=tuple(SmokeMode))
    parser.add_argument("--identity-url", default="http://127.0.0.1:8080")
    parser.add_argument("--catalog-url", default="http://127.0.0.1:8081")
    parser.add_argument("--search-url", default="http://127.0.0.1:8085")
    parser.add_argument("--public-origin", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--state", type=state_path, default=state_path("deploy/results/backend-smoke.json")
    )
    parser.add_argument(
        "--release-env", type=Path, help="Optional ignored Compose env for DB erasure proof"
    )
    parser.add_argument("--timeout", type=int, default=SmokeLimit.WAIT_SECONDS)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout_must_be_positive")
    configuration = SmokeConfiguration(
        identity_url=args.identity_url.rstrip("/"),
        catalog_url=args.catalog_url.rstrip("/"),
        search_url=args.search_url.rstrip("/"),
        origin=args.public_origin,
        state_path=args.state,
        release_env=args.release_env,
        timeout=args.timeout,
    )
    mode = SmokeMode(args.mode)
    try:
        if mode is SmokeMode.PREPARE:
            fixture_id = uuid4()
            state = SmokeState(
                fixture_id=fixture_id,
                login=f"smoke_{fixture_id.hex[: SmokeLimit.LOGIN_SUFFIX]}",
                password=token_urlsafe(SmokeLimit.PASSWORD_BYTES),
            )
            write_state(configuration.state_path, state, create=True)
        else:
            state = SmokeState.model_validate_json(
                configuration.state_path.read_text(encoding="utf-8")
            )
        with httpx.Client(timeout=SmokeLimit.HTTP_SECONDS, follow_redirects=False) as client:
            if mode is SmokeMode.PREPARE:
                prepare(client, configuration, state)
            elif mode is SmokeMode.VERIFY:
                verify(client, configuration, state)
            else:
                cleanup(client, configuration, state)
    except (
        httpx.HTTPError,
        SQLAlchemyError,
        ValidationError,
        OSError,
        ValueError,
        KeyError,
    ) as error:
        raise SystemExit(f"backend_smoke_failed_{type(error).__name__}") from None
    except SmokeError as error:
        raise SystemExit(str(error)) from None
    stdout.write(f"backend_smoke_{mode}_passed\n")


if __name__ == "__main__":
    main()
