"""Одноразовый административный процесс миграций Search."""

from pathlib import Path

from alembic import command
from alembic.config import Config

from memes_search.infrastructure.configuration import Settings


def main() -> None:
    """Применить миграции тем же артефактом и окружением, что сервис."""
    settings = Settings()
    configuration = Config()
    configuration.set_main_option(
        "script_location", str(Path(__file__).resolve().with_name("migrations"))
    )
    configuration.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
    command.upgrade(configuration, "head")
