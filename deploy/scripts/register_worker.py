"""Зарегистрировать LAN-воркер и сохранить настройки без вывода секретов."""

import argparse
from enum import IntEnum, StrEnum
from getpass import getpass
from ipaddress import IPv4Address
from os import O_CREAT, O_EXCL, O_WRONLY, environ, fdopen
from os import open as os_open
from pathlib import Path
from re import fullmatch
from stat import S_IRUSR, S_IWUSR
from uuid import UUID

import httpx


class RegistrationSetting(StrEnum):
    """Имена окружения и стабильного REST-контракта."""

    TOKEN_ENV = "MEMES_OPERATOR_ACCESS_TOKEN"  # noqa: S105
    RELEASE_ENV = "MEMES_RELEASE_ID"
    IDENTITY_ENV = "MEMES_IDENTITY_URL"
    PROCESSING_ENV = "MEMES_PROCESSING_URL"
    IDENTITY_URL = "http://identity:8080"
    PROCESSING_URL = "http://processing:8080"
    WORKER_ID = "worker_id"
    WORKER_TOKEN = "token"  # noqa: S105
    AUTHORIZATION = "Authorization"
    ORIGIN = "Origin"
    ENDPOINT = "/v1/workers"
    LOGIN_ENDPOINT = "/v1/auth/login"
    ACCESS_TOKEN = "access_token"  # noqa: S105
    OUTPUT = "deploy/.env.worker"
    BAD_RESPONSE = "invalid_worker_registration_response"
    RELEASE_PATTERN = r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}"
    SECRET_PATTERN = r"[a-zA-Z0-9_-]{32,}"  # noqa: S105
    OPERATIONS = "preview,ocr,transcript,visual_embedding,audio_embedding,text_embedding"


class RegistrationLimit(IntEnum):
    """Транспортные границы и стандартные LAN-порты."""

    TIMEOUT_SECONDS = 15
    FRONTEND_PORT = 5173
    PROCESSING_PORT = 8082


def authenticate(client: httpx.Client, *, identity_url: str, origin: str, login: str) -> str:
    """Получить токен оператора, запросив пароль скрытым вводом.

    Args:
        client: REST-транспорт административного процесса.
        identity_url: Адрес Identity.
        origin: Разрешённый :http:header:`Origin` фронтенда.
        login: Логин оператора.

    Returns:
        str: Короткоживущий токен, который не записывается на диск.
    """
    response = client.post(
        identity_url.rstrip("/") + RegistrationSetting.LOGIN_ENDPOINT,
        headers={RegistrationSetting.ORIGIN.value: origin},
        json={"login": login, "password": getpass("Пароль оператора: ")},
    )
    response.raise_for_status()
    payload: object = response.json()
    if not isinstance(payload, dict):
        raise ValueError(RegistrationSetting.BAD_RESPONSE)
    token = payload.get(RegistrationSetting.ACCESS_TOKEN)
    if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
        raise ValueError(RegistrationSetting.BAD_RESPONSE)
    return token


def worker_environment(
    payload: object, *, server_host: IPv4Address | None, release_id: str | None
) -> str:
    """Сформировать отдельное окружение устройства без серверных credentials.

    Args:
        payload: Ответ Processing на регистрацию устройства.
        server_host: IPv4 сервера для исходящих подключений воркера.
        release_id: Идентификатор образа той же версии кода.

    Returns:
        str: Содержимое приватного файла окружения воркера.
    """
    if not isinstance(payload, dict):
        raise ValueError(RegistrationSetting.BAD_RESPONSE)
    worker_id = payload.get(RegistrationSetting.WORKER_ID)
    secret = payload.get(RegistrationSetting.WORKER_TOKEN)
    if not isinstance(worker_id, str) or not isinstance(secret, str):
        raise ValueError(RegistrationSetting.BAD_RESPONSE)
    identifier = UUID(worker_id)
    if fullmatch(RegistrationSetting.SECRET_PATTERN, secret) is None:
        raise ValueError(RegistrationSetting.BAD_RESPONSE)
    lines = [f"MEMES_WORKER_WORKER_ID={identifier}", f"MEMES_WORKER_TOKEN={secret}"]
    if server_host is not None:
        if release_id is None or fullmatch(RegistrationSetting.RELEASE_PATTERN, release_id) is None:
            raise ValueError("valid_release_id_required")
        lines.extend(
            (
                f"MEMES_WORKER_RELEASE_ID={release_id}",
                f"MEMES_WORKER_COORDINATOR_URL=http://{server_host}:{RegistrationLimit.PROCESSING_PORT}",
                "MEMES_WORKER_ALLOW_INSECURE_HTTP=true",
                "MEMES_WORKER_DEVICE=cpu",
                f"MEMES_WORKER_OPERATIONS={RegistrationSetting.OPERATIONS}",
                "MEMES_WORKER_MAXIMUM_PARALLEL_TASKS=1",
                "MEMES_WORKER_BUILD_TARGET=worker-ml",
                "MEMES_WORKER_MODELS_DIRECTORY=./models",
            )
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    """Выполнить вход, регистрацию и запись конфигурации одним CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--processing-url",
        default=environ.get(RegistrationSetting.PROCESSING_ENV, RegistrationSetting.PROCESSING_URL),
    )
    parser.add_argument(
        "--identity-url",
        default=environ.get(RegistrationSetting.IDENTITY_ENV, RegistrationSetting.IDENTITY_URL),
    )
    parser.add_argument("--login")
    parser.add_argument("--origin")
    parser.add_argument("--server-host", type=IPv4Address)
    parser.add_argument("--release-id", default=environ.get(RegistrationSetting.RELEASE_ENV))
    parser.add_argument("--name", required=True)
    parser.add_argument("--output", type=Path, default=Path(RegistrationSetting.OUTPUT))
    arguments = parser.parse_args()
    if arguments.server_host is not None and (
        not arguments.release_id
        or fullmatch(RegistrationSetting.RELEASE_PATTERN, arguments.release_id) is None
    ):
        parser.error("--server-host requires --release-id or MEMES_RELEASE_ID")
    token = environ.get(RegistrationSetting.TOKEN_ENV)
    origin = arguments.origin
    if origin is None and arguments.server_host is not None:
        origin = f"http://{arguments.server_host}:{RegistrationLimit.FRONTEND_PORT}"
    if arguments.login and not origin:
        parser.error("--login requires --origin or --server-host")
    if not arguments.login and not token:
        parser.error("--login or MEMES_OPERATOR_ACCESS_TOKEN is required")
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os_open(arguments.output, O_WRONLY | O_CREAT | O_EXCL, S_IRUSR | S_IWUSR)
    except FileExistsError:
        parser.error("output already exists; use a new file for a new worker")
    completed = False
    try:
        with (
            fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output,
            httpx.Client(
                timeout=RegistrationLimit.TIMEOUT_SECONDS, follow_redirects=False
            ) as client,
        ):
            if arguments.login:
                token = authenticate(
                    client,
                    identity_url=arguments.identity_url,
                    origin=origin,
                    login=arguments.login,
                )
            response = client.post(
                arguments.processing_url.rstrip("/") + RegistrationSetting.ENDPOINT,
                headers={RegistrationSetting.AUTHORIZATION.value: f"Bearer {token}"},
                json={"name": arguments.name},
            )
            response.raise_for_status()
            output.write(
                worker_environment(
                    response.json(),
                    server_host=arguments.server_host,
                    release_id=arguments.release_id,
                )
            )
        completed = True
    finally:
        if not completed:
            arguments.output.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
