"""Подготовка отдельных integration баз на локальном disposable стенде."""

import argparse
import asyncio
from enum import IntEnum, StrEnum
from pathlib import Path

from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine


class TestService(StrEnum):
    """Сервисы со строго отдельными тестовыми базами."""

    IDENTITY = "identity"
    CATALOG = "catalog"
    PROCESSING = "processing"
    SEARCH = "search"


class TestDefault(IntEnum):
    """Порты isolated Compose и ограничение подключения."""

    POSTGRES_PORT = 55433
    S3_PORT = 58334
    TIMEOUT_SECONDS = 10


class TestSetting(StrEnum):
    """Предопределённые ресурсы одноразового CI стенда."""

    HOST = "127.0.0.1"
    ADMIN = "memes_admin"
    ADMIN_DATABASE = "postgres"
    SCHEME = "postgresql+asyncpg"
    ENV_FILE = "deploy/.env"
    OUTPUT = "deploy/results/test-environment.env"


async def prepare(source: Path, output: Path) -> None:
    """Создать только *_test базы и сохранить секреты без stdout.

    Args:
        source: Только файл формата generate_env.py.
        output: Закрытый файл переменных интеграционных проверок.
    """
    settings = dict(
        line.split("=", 1)
        for line in source.read_text().splitlines()
        if line and not line.startswith("#")
    )
    port = int(settings.get("MEMES_POSTGRES_PORT", str(TestDefault.POSTGRES_PORT)))
    url = URL.create(
        "postgresql+asyncpg",
        username=TestSetting.ADMIN.value,
        password=settings["POSTGRES_PASSWORD"],
        host=TestSetting.HOST.value,
        port=port,
        database=TestSetting.ADMIN_DATABASE.value,
    )
    administrative = create_async_engine(
        url,
        isolation_level="AUTOCOMMIT",
        hide_parameters=True,
        connect_args={"timeout": TestDefault.TIMEOUT_SECONDS},
    )
    try:
        async with administrative.connect() as connection:
            for service in TestService:
                database = f"memes_{service}_test"
                exists = await connection.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname=:database"),
                    {"database": database},
                )
                if not exists:
                    await connection.execute(text(f'CREATE DATABASE "{database}"'))
    finally:
        await administrative.dispose()
    search = create_async_engine(
        url.set(database="memes_search_test"),
        hide_parameters=True,
        connect_args={"timeout": TestDefault.TIMEOUT_SECONDS},
    )
    try:
        async with search.begin() as connection:
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    finally:
        await search.dispose()
    lines = []
    for service in TestService:
        prefix = (
            "MEMES_TEST_IDENTITY"
            if service is TestService.IDENTITY
            else f"MEMES_{service.upper()}_TEST"
        )
        lines.append(
            f"{prefix}_DATABASE_URL={TestSetting.SCHEME}://{TestSetting.ADMIN}:"
            f"{settings['POSTGRES_PASSWORD']}@{TestSetting.HOST}:{port}/memes_{service}_test"
        )
    endpoint = (
        f"http://{TestSetting.HOST}:{settings.get('MEMES_S3_PORT', str(TestDefault.S3_PORT))}"
    )
    for service in (TestService.CATALOG, TestService.SEARCH):
        prefix = f"MEMES_{service.upper()}_TEST_S3"
        lines.extend(
            (
                f"{prefix}_ENDPOINT_URL={endpoint}",
                f"{prefix}_BUCKET={settings['MEMES_S3_BUCKET']}",
                f"{prefix}_ACCESS_KEY={settings['MEMES_S3_ACCESS_KEY']}",
                f"{prefix}_SECRET_KEY={settings['MEMES_S3_SECRET_KEY']}",
            )
        )
    lines.extend(
        (
            f"MEMES_TEST_S3_ENDPOINT_URL={endpoint}",
            f"MEMES_TEST_S3_ACCESS_KEY={settings['MEMES_S3_ACCESS_KEY']}",
            f"MEMES_TEST_S3_SECRET_KEY={settings['MEMES_S3_SECRET_KEY']}",
            "MEMES_REQUIRE_INTEGRATION=1",
        )
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as result:
        result.write("\n".join(lines) + "\n")
    output.chmod(0o600)


def main() -> None:
    """Выполнить подготовку только явно указанного disposable стенда."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(TestSetting.ENV_FILE))
    parser.add_argument("--output", type=Path, default=Path(TestSetting.OUTPUT))
    arguments = parser.parse_args()
    asyncio.run(prepare(arguments.env_file, arguments.output))


if __name__ == "__main__":
    main()
