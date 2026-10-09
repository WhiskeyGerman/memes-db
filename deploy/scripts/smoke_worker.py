"""Сквозной smoke реального Worker: собственные REST/S3 fixtures и безопасная очистка."""

import argparse
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from http import HTTPStatus
from io import BytesIO
from os import O_CREAT, O_EXCL, O_WRONLY, environ, fdopen
from os import open as os_open
from pathlib import Path
from secrets import token_urlsafe
from shutil import which
from stat import S_IRUSR, S_IWUSR
from subprocess import TimeoutExpired, run
from sys import stdout
from time import monotonic, sleep
from uuid import UUID, uuid4

import httpx
from PIL import Image, ImageDraw, ImageFont, PngImagePlugin
from pydantic import Field, ValidationError

from deploy.scripts.smoke_backend import (
    SmokeConfiguration,
    SmokeError,
    SmokeLimit,
    SmokeMode,
    SmokeState,
    bearer,
    expect,
    object_body,
    state_path,
    string_field,
    write_state,
)


class WorkerLimit(IntEnum):
    """Конечные границы измерения без утверждений о производительности."""

    TIMEOUT_SECONDS = 900
    ADMIN_SECONDS = 120
    IMAGE_WIDTH = 800
    IMAGE_HEIGHT = 400
    FONT_SIZE = 64
    TEXT_LEFT = 60
    TEXT_TOP = 140
    PAGE_SIZE = 100


class WorkerFeature(StrEnum):
    """Обязательные реальные стадии изображения с читаемым текстом."""

    PREVIEW = "preview"
    OCR = "ocr"
    VISUAL = "visual_embedding"
    TEXT = "text_embedding"


class WorkerStage(StrEnum):
    """Состояния транспортного контракта обработки."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    AVAILABLE = "available"


class WorkerState(SmokeState):
    """Закрытый checkpoint только своих operator, device, account и media."""

    operator: SmokeState = Field(repr=False)
    worker_id: UUID | None = None
    worker_token: str | None = Field(default=None, repr=False)
    device_revoked: bool = False


@dataclass(frozen=True, slots=True)
class WorkerConfiguration:
    """Конкретные адреса стенда и одноразового административного процесса."""

    api: SmokeConfiguration
    processing_url: str
    compose_env: Path
    compose_file: Path


def save(configuration: WorkerConfiguration, state: WorkerState) -> None:
    """Сохранить полный checkpoint после каждой подтверждённой операции."""
    write_state(configuration.api.state_path, state)


def authenticate_account(
    client: httpx.Client,
    configuration: WorkerConfiguration,
    state: WorkerState,
    account: SmokeState,
    *,
    register: bool = False,
) -> bool:
    """Создать свой account либо обновить access; отсутствие допустимо при cleanup."""
    response = client.post(
        configuration.api.identity_url + ("/v1/auth/register" if register else "/v1/auth/login"),
        headers={"Origin": configuration.api.origin},
        json={"login": account.login, "password": account.password},
    )
    if not register and response.status_code == HTTPStatus.UNAUTHORIZED:
        return False
    account.access_token = string_field(
        object_body(expect(response, HTTPStatus.CREATED if register else HTTPStatus.OK)),
        "access_token",
    )
    me = object_body(
        expect(
            client.get(configuration.api.identity_url + "/v1/auth/me", headers=bearer(account)),
            HTTPStatus.OK,
        )
    )
    owner = UUID(string_field(me, "user_id"))
    if account.owner_id is not None and account.owner_id != owner:
        raise SmokeError("worker_smoke_account_identity_changed")
    account.owner_id = owner
    save(configuration, state)
    return True


def bootstrap_operator(configuration: WorkerConfiguration, state: WorkerState) -> None:
    """Вызвать штатный admin CLI; пароль передать только окружением процесса."""
    docker = which("docker")
    if docker is None:
        raise SmokeError("worker_smoke_docker_required")
    completed = run(  # noqa: S603
        [
            docker,
            "compose",
            "--env-file",
            str(configuration.compose_env),
            "-f",
            str(configuration.compose_file),
            "run",
            "--no-deps",
            "--rm",
            "-T",
            "-e",
            "MEMES_BOOTSTRAP_PASSWORD",
            "identity",
            "python",
            "-m",
            "memes_identity.presentation.admin",
            "operator",
            "--login",
            state.operator.login,
        ],
        env={**environ, "MEMES_BOOTSTRAP_PASSWORD": state.operator.password},
        capture_output=True,
        timeout=WorkerLimit.ADMIN_SECONDS,
        check=False,
    )
    if completed.returncode:
        raise SmokeError("worker_smoke_operator_bootstrap_failed")


def worker_env_path(configuration: WorkerConfiguration) -> Path:
    """Разместить device secret только рядом с ignored checkpoint."""
    return configuration.api.state_path.with_suffix(".worker.env")


def write_worker_env(configuration: WorkerConfiguration, state: WorkerState) -> None:
    """Эксклюзивно записать только device credentials для запуска ML Worker."""
    if state.worker_id is None or state.worker_token is None:
        raise SmokeError("worker_smoke_device_missing")
    if "\n" in state.worker_token or "\r" in state.worker_token:
        raise SmokeError("worker_smoke_invalid_device_token")
    with fdopen(
        os_open(worker_env_path(configuration), O_WRONLY | O_CREAT | O_EXCL, S_IRUSR | S_IWUSR),
        "w",
        encoding="utf-8",
        newline="\n",
    ) as output:
        output.write(
            f"MEMES_WORKER_WORKER_ID={state.worker_id}\nMEMES_WORKER_TOKEN={state.worker_token}\n"
        )


def fixture_png(identifier: UUID) -> bytes:
    """Создать крупный читаемый OCR текст и уникальные байты стандартного PNG."""
    image = Image.new("RGB", (WorkerLimit.IMAGE_WIDTH, WorkerLimit.IMAGE_HEIGHT), "white")
    ImageDraw.Draw(image).text(
        (WorkerLimit.TEXT_LEFT, WorkerLimit.TEXT_TOP),
        "MEME 123",
        fill="black",
        font=ImageFont.load_default(size=WorkerLimit.FONT_SIZE),
    )
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("worker-smoke-fixture", str(identifier))
    with BytesIO() as output:
        image.save(output, format="PNG", pnginfo=metadata)
        return output.getvalue()


def upload_image(
    client: httpx.Client, configuration: WorkerConfiguration, state: WorkerState
) -> None:
    """Загрузить свой PNG через настоящий signed POST S3 и Catalog complete."""
    data = fixture_png(state.fixture_id)
    ticket = object_body(
        expect(
            client.post(
                configuration.api.catalog_url + "/v1/uploads",
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
        raise SmokeError("worker_smoke_invalid_s3_fields")
    expect(
        client.post(
            string_field(ticket, "upload_url"),
            data=fields,
            files={"file": ("worker-smoke.png", data, "image/png")},
        ),
        HTTPStatus.NO_CONTENT,
        HTTPStatus.CREATED,
        HTTPStatus.OK,
    )
    completed = object_body(
        expect(
            client.post(
                configuration.api.catalog_url
                + f"/v1/uploads/{string_field(ticket, 'upload_id')}/complete",
                headers=bearer(state),
            ),
            HTTPStatus.CREATED,
        )
    )
    state.media_ids.append(UUID(string_field(completed, "media_id")))
    save(configuration, state)


def prepare(client: httpx.Client, configuration: WorkerConfiguration, state: WorkerState) -> None:
    """Подготовить свои ресурсы перед запуском Worker с сохранённым env-файлом."""
    bootstrap_operator(configuration, state)
    if not authenticate_account(client, configuration, state, state.operator):
        raise SmokeError("worker_smoke_operator_login_failed")
    authenticate_account(client, configuration, state, state, register=True)
    device = object_body(
        expect(
            client.post(
                configuration.processing_url + "/v1/workers",
                headers=bearer(state.operator),
                json={"name": f"smoke-{state.fixture_id}"},
            ),
            HTTPStatus.CREATED,
        )
    )
    state.worker_id = UUID(string_field(device, "worker_id"))
    state.worker_token = string_field(device, "token")
    save(configuration, state)
    write_worker_env(configuration, state)
    upload_image(client, configuration, state)


def image_ready(
    client: httpx.Client, configuration: WorkerConfiguration, state: WorkerState
) -> bool:
    """Проверить успешные реальные стадии и доставленную публичную проекцию."""
    if not state.media_ids:
        raise SmokeError("worker_smoke_image_missing")
    media_id = state.media_ids[0]
    processing = object_body(
        expect(
            client.get(
                configuration.processing_url + f"/v1/processing/{media_id}",
                headers=bearer(state.operator),
            ),
            HTTPStatus.OK,
        )
    )
    tasks = processing.get("tasks")
    if not isinstance(tasks, list):
        raise SmokeError("worker_smoke_invalid_tasks")
    latest: dict[str, tuple[int, str]] = {}
    for item in tasks:
        if not isinstance(item, dict):
            raise SmokeError("worker_smoke_invalid_task")
        kind, generation, status = item.get("kind"), item.get("generation"), item.get("state")
        if (
            not isinstance(kind, str)
            or not isinstance(generation, int)
            or not isinstance(status, str)
        ):
            raise SmokeError("worker_smoke_invalid_task_fields")
        if kind not in latest or generation >= latest[kind][0]:
            latest[kind] = (generation, status)
    if any(
        latest.get(kind, (0, ""))[1] in (WorkerStage.FAILED, WorkerStage.CANCELLED)
        for kind in WorkerFeature
    ):
        raise SmokeError("worker_smoke_real_processing_failed")
    if any(latest.get(kind, (0, ""))[1] != WorkerStage.SUCCEEDED for kind in WorkerFeature):
        return False
    media = object_body(
        expect(client.get(configuration.api.catalog_url + f"/v1/media/{media_id}"), HTTPStatus.OK)
    )
    features = media.get("features")
    if not isinstance(features, list):
        raise SmokeError("worker_smoke_invalid_catalog_features")
    ready = {
        item.get("kind")
        for item in features
        if isinstance(item, dict) and item.get("state") == WorkerStage.AVAILABLE
    }
    if not set(WorkerFeature).issubset(ready):
        return False
    preview = next(
        item
        for item in features
        if isinstance(item, dict) and item.get("kind") == WorkerFeature.PREVIEW
    )
    content = expect(client.get(string_field(preview, "url")), HTTPStatus.OK).content
    with Image.open(BytesIO(content)) as image:
        image.verify()
    return True


def semantic_self_hit(
    client: httpx.Client, configuration: WorkerConfiguration, state: WorkerState
) -> None:
    """Проверить реальный query encoder и выдачу своего изображения без fallback."""
    response = expect(
        client.post(
            configuration.api.search_url + "/v1/search",
            headers=bearer(state),
            json={
                "request_id": str(uuid4()),
                "mode": "semantic",
                "text": "MEME 123",
                "page_size": WorkerLimit.PAGE_SIZE,
            },
        ),
        HTTPStatus.OK,
        HTTPStatus.ACCEPTED,
    )
    body = object_body(response)
    resource = string_field(body, "resource_url")
    if not resource.startswith("/v1/search/indexes/"):
        raise SmokeError("worker_smoke_invalid_search_link")
    state.query_links.append(resource)
    save(configuration, state)
    deadline = monotonic() + configuration.api.timeout
    while response.status_code == HTTPStatus.ACCEPTED and monotonic() < deadline:
        sleep(SmokeLimit.POLL_SECONDS)
        response = expect(
            client.get(configuration.api.search_url + resource, headers=bearer(state)),
            HTTPStatus.OK,
            HTTPStatus.ACCEPTED,
        )
        body = object_body(response)
    if response.status_code != HTTPStatus.OK:
        raise SmokeError("worker_smoke_query_deadline_exceeded")
    results = body.get("results")
    if (
        body.get("degraded") is not False
        or not isinstance(results, list)
        or not any(
            isinstance(item, dict) and item.get("representative_id") == str(state.media_ids[0])
            for item in results
        )
    ):
        raise SmokeError("worker_smoke_semantic_self_hit_missing")


def verify(client: httpx.Client, configuration: WorkerConfiguration, state: WorkerState) -> None:
    """Дождаться реального Worker, S3 preview и семантического совпадения."""
    if state.deleted or not authenticate_account(client, configuration, state, state):
        raise SmokeError("worker_smoke_owner_missing")
    if not authenticate_account(client, configuration, state, state.operator):
        raise SmokeError("worker_smoke_operator_missing")
    deadline = monotonic() + configuration.api.timeout
    while monotonic() < deadline:
        if image_ready(client, configuration, state):
            semantic_self_hit(client, configuration, state)
            state.complete = True
            save(configuration, state)
            return
        sleep(SmokeLimit.POLL_SECONDS)
    raise SmokeError("worker_smoke_processing_deadline_exceeded")


def delete_account(
    client: httpx.Client,
    configuration: WorkerConfiguration,
    state: WorkerState,
    account: SmokeState,
) -> None:
    """Удалить только account с известным собственным паролем и его медиа."""
    if account.deleted:
        return
    if authenticate_account(client, configuration, state, account):
        expect(
            client.request(
                "DELETE",
                configuration.api.identity_url + "/v1/auth/account",
                headers=bearer(account),
                json={"password": account.password, "delete_media": True},
            ),
            HTTPStatus.NO_CONTENT,
        )
    account.deleted = True
    account.access_token = None
    save(configuration, state)


def cleanup(client: httpx.Client, configuration: WorkerConfiguration, state: WorkerState) -> None:
    """Отозвать только свой device и удалить собственные disposable аккаунты."""
    if state.worker_id is not None and not state.device_revoked:
        if not authenticate_account(client, configuration, state, state.operator):
            raise SmokeError("worker_smoke_operator_needed_for_revoke")
        expect(
            client.delete(
                configuration.processing_url + f"/v1/workers/{state.worker_id}",
                headers=bearer(state.operator),
            ),
            HTTPStatus.OK,
            HTTPStatus.NOT_FOUND,
        )
        state.device_revoked = True
        state.worker_token = None
        save(configuration, state)
        worker_env_path(configuration).unlink(missing_ok=True)
    delete_account(client, configuration, state, state)
    delete_account(client, configuration, state, state.operator)
    deadline = monotonic() + configuration.api.timeout
    while monotonic() < deadline:
        if all(
            client.get(configuration.api.catalog_url + f"/v1/media/{identifier}").status_code
            == HTTPStatus.NOT_FOUND
            for identifier in state.media_ids
        ):
            state.password = ""
            state.operator.password = ""
            save(configuration, state)
            return
        sleep(SmokeLimit.POLL_SECONDS)
    raise SmokeError("worker_smoke_cleanup_deadline_exceeded")


def main() -> None:
    """Исполнить фазу; credentials и HTTP body никогда не печатать."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=tuple(SmokeMode))
    parser.add_argument("--identity-url", default="http://127.0.0.1:8080")
    parser.add_argument("--catalog-url", default="http://127.0.0.1:8081")
    parser.add_argument("--processing-url", default="http://127.0.0.1:8082")
    parser.add_argument("--search-url", default="http://127.0.0.1:8083")
    parser.add_argument("--public-origin", default="http://127.0.0.1:8080")
    parser.add_argument("--compose-env", type=Path, default=Path("deploy/.env"))
    parser.add_argument("--compose-file", type=Path, default=Path("deploy/compose.yaml"))
    parser.add_argument(
        "--state", type=state_path, default=state_path("deploy/results/worker-smoke.json")
    )
    parser.add_argument("--timeout", type=int, default=WorkerLimit.TIMEOUT_SECONDS)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout_must_be_positive")
    configuration = WorkerConfiguration(
        api=SmokeConfiguration(
            identity_url=args.identity_url.rstrip("/"),
            catalog_url=args.catalog_url.rstrip("/"),
            search_url=args.search_url.rstrip("/"),
            origin=args.public_origin,
            state_path=args.state,
            release_env=None,
            timeout=args.timeout,
        ),
        processing_url=args.processing_url.rstrip("/"),
        compose_env=args.compose_env.resolve(),
        compose_file=args.compose_file.resolve(),
    )
    mode = SmokeMode(args.mode)
    try:
        if mode is SmokeMode.PREPARE:
            identifier = uuid4()
            state = WorkerState(
                fixture_id=identifier,
                login=f"wsm_{identifier.hex[: SmokeLimit.LOGIN_SUFFIX]}",
                password=token_urlsafe(SmokeLimit.PASSWORD_BYTES),
                operator=SmokeState(
                    fixture_id=identifier,
                    login=f"wop_{identifier.hex[: SmokeLimit.LOGIN_SUFFIX]}",
                    password=token_urlsafe(SmokeLimit.PASSWORD_BYTES),
                ),
            )
            write_state(configuration.api.state_path, state, create=True)
        else:
            state = WorkerState.model_validate_json(
                configuration.api.state_path.read_text(encoding="utf-8")
            )
        with httpx.Client(timeout=SmokeLimit.HTTP_SECONDS, follow_redirects=False) as client:
            if mode is SmokeMode.PREPARE:
                prepare(client, configuration, state)
            elif mode is SmokeMode.VERIFY:
                verify(client, configuration, state)
            else:
                cleanup(client, configuration, state)
    except (httpx.HTTPError, ValidationError, OSError, ValueError, TimeoutExpired) as error:
        raise SystemExit(f"worker_smoke_failed_{type(error).__name__}") from None
    except SmokeError as error:
        raise SystemExit(str(error)) from None
    stdout.write(f"worker_smoke_{mode}_passed\n")


if __name__ == "__main__":
    main()
