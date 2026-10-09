"""Создать локальную или LAN release-конфигурацию без публикации секретов."""

import argparse
import json
from enum import IntEnum, StrEnum
from os import O_CREAT, O_EXCL, O_WRONLY, fdopen
from os import open as os_open
from pathlib import Path
from re import fullmatch
from secrets import token_hex
from stat import S_IRUSR, S_IWUSR


class ConfigurationLimit(IntEnum):
    """Границы секретов и публичных имён конфигурации."""

    SECRET_BYTES = 32
    HOSTNAME_MAXIMUM = 253


class PublicPort(IntEnum):
    """Стандартные опубликованные порты локального стенда."""

    FRONTEND = 5173
    IDENTITY = 8080
    CATALOG = 8081
    SEARCH = 8083
    S3 = 58334
    POSTGRES = 55433


class EnvironmentDefault(StrEnum):
    """Исходные значения локального развёртывания."""

    FILE = "deploy/.env"
    DATABASE_HOST = "postgres"
    LOCAL_HOST = "localhost"
    LOCAL_S3_HOST = "host.docker.internal"
    LOCAL_BIND = "127.0.0.1"
    NETWORK_BIND = "0.0.0.0"  # noqa: S104 -- Явно выбранный LAN-режим.
    HOST_PATTERN = (
        r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*"
        r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    )
    RELEASE_PATTERN = r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}"
    BUCKET = "memes-media"
    JOURNAL_BUCKET = "memes-deletion-journal"
    EMBEDDING_VERSION = (
        '{"model":"jinaai/jina-embeddings-v5-omni-nano",'
        '"weights":"009e0d09a98a82227526e8c5aa7b0aa282e36163",'
        '"task_adapter":"retrieval-v1","preprocessing":"ffmpeg-sampling-v1",'
        '"space":"jina-v5-omni-nano","dimensions":768}'
    )
    LOCAL_ORIGINS = (
        '["http://localhost:8080","http://localhost:8081","http://localhost:8083",'
        '"http://127.0.0.1:8080","http://localhost:5173","http://localhost:3000"]'
    )


def public_hostname(value: str) -> str:
    """Проверить hostname без схемы, порта и управляющих символов.

    Args:
        value: IPv4-адрес или DNS-имя сервера.

    Returns:
        str: Проверенное имя, пригодное для URL и env-файла.

    Raises:
        argparse.ArgumentTypeError: Имя содержит недопустимые символы.
    """
    if (
        len(value) > ConfigurationLimit.HOSTNAME_MAXIMUM
        or fullmatch(EnvironmentDefault.HOST_PATTERN, value) is None
    ):
        raise argparse.ArgumentTypeError("--public-host requires an IPv4 address or DNS hostname")
    return value


def main() -> None:
    """Создать новый env-файл; существующая конфигурация не перезаписывается."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--output", type=Path, default=Path(EnvironmentDefault.FILE))
    parser.add_argument("--public-host", type=public_hostname)
    arguments = parser.parse_args()
    if fullmatch(EnvironmentDefault.RELEASE_PATTERN, arguments.release_id) is None:
        parser.error("--release-id must be a valid image tag (1-128 ASCII characters)")
    public_host = arguments.public_host or EnvironmentDefault.LOCAL_HOST
    s3_host = arguments.public_host or EnvironmentDefault.LOCAL_S3_HOST
    bind_host = (
        EnvironmentDefault.NETWORK_BIND if arguments.public_host else EnvironmentDefault.LOCAL_BIND
    )
    origins: list[str] = json.loads(EnvironmentDefault.LOCAL_ORIGINS)
    origins.extend(
        f"http://{public_host}:{port}"
        for port in (
            PublicPort.FRONTEND,
            PublicPort.IDENTITY,
            PublicPort.CATALOG,
            PublicPort.SEARCH,
        )
    )
    allowed_origins = json.dumps(list(dict.fromkeys(origins)), separators=(",", ":"))
    postgres = token_hex(ConfigurationLimit.SECRET_BYTES)
    internal = token_hex(ConfigurationLimit.SECRET_BYTES)
    s3_key = token_hex(ConfigurationLimit.SECRET_BYTES)
    s3_secret = token_hex(ConfigurationLimit.SECRET_BYTES)
    passwords = {
        service: token_hex(ConfigurationLimit.SECRET_BYTES)
        for service in ("identity", "catalog", "processing", "search")
    }
    lines = [
        f"MEMES_RELEASE_ID={arguments.release_id}",
        f"POSTGRES_PASSWORD={postgres}",
        f"MEMES_INTERNAL_API_KEY={internal}",
        f"MEMES_S3_ACCESS_KEY={s3_key}",
        f"MEMES_S3_SECRET_KEY={s3_secret}",
        f"MEMES_S3_BUCKET={EnvironmentDefault.BUCKET}",
        f"MEMES_JOURNAL_BUCKET={EnvironmentDefault.JOURNAL_BUCKET}",
        f"MEMES_S3_PORT={PublicPort.S3}",
        f"MEMES_POSTGRES_PORT={PublicPort.POSTGRES}",
        f"MEMES_S3_PUBLIC_ENDPOINT_URL=http://{s3_host}:{PublicPort.S3}",
        f"MEMES_API_PUBLISH_HOST={bind_host}",
        f"MEMES_FRONTEND_PUBLISH_HOST={bind_host}",
        f"MEMES_FRONTEND_PORT={PublicPort.FRONTEND}",
        f"MEMES_FRONTEND_IDENTITY_URL=http://{public_host}:{PublicPort.IDENTITY}",
        f"MEMES_FRONTEND_CATALOG_URL=http://{public_host}:{PublicPort.CATALOG}",
        f"MEMES_FRONTEND_SEARCH_URL=http://{public_host}:{PublicPort.SEARCH}",
        f"MEMES_CSRF_ALLOWED_ORIGINS='{allowed_origins}'",
        f"MEMES_CORS_ALLOWED_ORIGINS='{allowed_origins}'",
        "MEMES_COOKIE_SECURE=false",
        "MEMES_ALLOW_INSECURE_HTTP=true",
        f"MEMES_ACCEPTED_EMBEDDING_VERSION='{EnvironmentDefault.EMBEDDING_VERSION}'",
        "MEMES_WORKER_DEVICE=cpu",
        "MEMES_WORKER_OPERATIONS=preview",
        "MEMES_WORKER_MODELS_DIRECTORY=./models",
    ]
    for service, password in passwords.items():
        lines.extend(
            (
                f"{service.upper()}_DATABASE_PASSWORD={password}",
                f"MEMES_{service.upper()}_DATABASE_URL=postgresql+asyncpg://memes_{service}:{password}@{EnvironmentDefault.DATABASE_HOST}:5432/memes_{service}",
            )
        )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    with fdopen(
        os_open(arguments.output, O_WRONLY | O_CREAT | O_EXCL, S_IRUSR | S_IWUSR),
        "w",
        encoding="utf-8",
        newline="\n",
    ) as output:
        output.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
