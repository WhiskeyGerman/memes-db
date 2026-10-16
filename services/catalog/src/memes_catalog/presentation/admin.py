"""Одноразовые администрирование и восстановление текущим артефактом релиза."""

import argparse
import asyncio
import logging
from datetime import datetime
from enum import StrEnum

from memes_catalog.application.dto.catalog import CatalogDefault, SearchIndexTarget
from memes_catalog.infrastructure.configuration import CatalogSettings
from memes_catalog.presentation.main import build_container, close_container
from memes_platform.infrastructure.database import create_engine
from memes_platform.infrastructure.logging import ServiceName, configure_logging


class AdminCommand(StrEnum):
    """Разрешённые отдельные process types."""

    REPLAY = "replay-deletions"
    REINDEX = "reindex-search"


logger = logging.getLogger(__name__)


async def execute(command: AdminCommand, since: datetime | None, target: SearchIndexTarget) -> None:
    """Запустить отдельную операцию без HTTP и неявной миграции.

    Args:
        command: Выбранная команда.
        since: Граница журнала восстановления.
        target: Индекс для административного построения.
    """
    settings = CatalogSettings()
    configure_logging(
        level=settings.log_level,
        release_id=settings.release_id,
        service_name=ServiceName.CATALOG,
        environment=settings.deployment_environment,
    )
    engine = create_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_pool_overflow,
    )
    dependencies = build_container(settings, engine)
    try:
        if command is AdminCommand.REPLAY:
            await dependencies.catalog.replay_deletions(since=since)
            while await dependencies.catalog.maintenance(
                now=dependencies.catalog.clock.now(), limit=CatalogDefault.OUTBOX_BATCH
            ):
                continue
        else:
            await dependencies.catalog.reindex_search(
                batch_size=CatalogDefault.BATCH_SIZE, target=target
            )
        logger.info("catalog_admin_completed")
    finally:
        await close_container(dependencies)
        await engine.dispose()


def main() -> None:
    """Принять операторский argv для текущего build artifact."""
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=list(AdminCommand))
    parser.add_argument("--since", type=datetime.fromisoformat)
    parser.add_argument(
        "--target", choices=list(SearchIndexTarget), default=SearchIndexTarget.PRIMARY
    )
    arguments = parser.parse_args()
    asyncio.run(
        execute(
            AdminCommand(arguments.command), arguments.since, SearchIndexTarget(arguments.target)
        )
    )


if __name__ == "__main__":
    main()
