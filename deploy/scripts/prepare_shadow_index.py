"""Подготовка отдельной Search DB локального disposable release стенда."""

import argparse
import asyncio
from enum import IntEnum, StrEnum
from pathlib import Path
from re import fullmatch

from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine


class ShadowDefault(IntEnum):
    """Параметры подключения только локального Compose профиля."""

    POSTGRES_PORT = 55433
    TIMEOUT_SECONDS = 10


class ShadowSetting(StrEnum):
    """Задекларированные имена release ресурсов."""

    ENV_FILE = "deploy/.env"
    HOST = "127.0.0.1"
    ADMIN = "memes_admin"
    ADMIN_DATABASE = "postgres"
    DATABASE = "memes_search_shadow"
    OWNER = "memes_search"
    DATABASE_PATTERN = r"memes_search_[a-z][a-z0-9_]{0,47}"
    DATABASE_KEY = "MEMES_SEARCH_SHADOW_DATABASE_URL"
    PASSPORT_KEY = "MEMES_SHADOW_ACCEPTED_EMBEDDING_VERSION"


async def prepare(source: Path, database: str) -> None:
    """Создать новый индекс без изменения таблиц существующего Search.

    Args:
        source: Закрытый файл, созданный :mod:`generate_env`.
        database: Отдельное имя базы поискового индекса.

    Raises:
        ValueError: Ресурс имеет другого владельца или конфликтует с конфигурацией.
    """
    if fullmatch(ShadowSetting.DATABASE_PATTERN, database) is None:
        raise ValueError("shadow_database_name_invalid")
    original = source.read_text(encoding="utf-8")
    settings = dict(
        line.split("=", 1) for line in original.splitlines() if line and not line.startswith("#")
    )
    expected = (
        "postgresql+asyncpg://memes_search:"
        f"{settings['SEARCH_DATABASE_PASSWORD']}@postgres:5432/{database}"
    )
    configured = settings.get(ShadowSetting.DATABASE_KEY)
    if configured is not None and configured != expected:
        raise ValueError("shadow_database_conflicts_with_release_configuration")
    admin_url = URL.create(
        "postgresql+asyncpg",
        username=ShadowSetting.ADMIN.value,
        password=settings["POSTGRES_PASSWORD"],
        host=ShadowSetting.HOST.value,
        port=int(settings.get("MEMES_POSTGRES_PORT", str(ShadowDefault.POSTGRES_PORT))),
        database=ShadowSetting.ADMIN_DATABASE.value,
    )
    administrative = create_async_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
        connect_args={"timeout": ShadowDefault.TIMEOUT_SECONDS},
    )
    try:
        async with administrative.connect() as connection:
            owner = await connection.scalar(
                text("SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=:database"),
                {"database": database},
            )
            if owner is None:
                await connection.execute(text(f'CREATE DATABASE "{database}" OWNER memes_search'))
            elif owner != ShadowSetting.OWNER:
                raise ValueError("shadow_database_owner_mismatch")
    finally:
        await administrative.dispose()
    shadow = create_async_engine(
        admin_url.set(database=database),
        hide_parameters=True,
        connect_args={"timeout": ShadowDefault.TIMEOUT_SECONDS},
    )
    try:
        async with shadow.begin() as connection:
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    finally:
        await shadow.dispose()
    additions = []
    if configured is None:
        additions.append(f"{ShadowSetting.DATABASE_KEY}={expected}")
    if ShadowSetting.PASSPORT_KEY not in settings:
        additions.append(
            f"{ShadowSetting.PASSPORT_KEY}={settings['MEMES_ACCEPTED_EMBEDDING_VERSION']}"
        )
    if additions:
        with source.open("a", encoding="utf-8") as output:
            output.write("\n".join(additions) + "\n")


def main() -> None:
    """Подготовить one-off database resource до миграции candidate релиза."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(ShadowSetting.ENV_FILE))
    parser.add_argument("--database", default=ShadowSetting.DATABASE.value)
    arguments = parser.parse_args()
    asyncio.run(prepare(arguments.env_file, arguments.database))


if __name__ == "__main__":
    main()
