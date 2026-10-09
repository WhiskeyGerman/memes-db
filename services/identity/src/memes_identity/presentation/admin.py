"""Одноразовые административные задачи в окружении сервиса."""

import argparse
import asyncio
import logging
from enum import StrEnum
from getpass import getpass
from os import environ

import httpx

from memes_identity.application.dto.authentication import PasswordPolicy
from memes_identity.application.dto.outbox import DeletionDestination, DeletionRecipient
from memes_identity.application.use_cases.accounts import AccountUseCases
from memes_identity.application.use_cases.administration import (
    IdentityMaintenanceUseCases,
    OperatorUseCases,
)
from memes_identity.application.use_cases.deletion_baseline import SeedShadowSearchDeletions
from memes_identity.infrastructure.authentication.passwords import Argon2PasswordVerifier
from memes_identity.infrastructure.clients.account_deletion import RestAccountDeletionPublisher
from memes_identity.infrastructure.clock import SystemClock
from memes_identity.infrastructure.configuration import IdentitySettings
from memes_identity.infrastructure.persistence.deletion_baseline import SqlDeletedOwnerSource
from memes_identity.infrastructure.persistence.maintenance import SqlIdentityMaintenance
from memes_identity.infrastructure.persistence.unit_of_work import SqlIdentityUnitOfWork
from memes_platform.infrastructure.database import create_engine, session_factory
from memes_platform.infrastructure.deletion_journal import S3DeletionJournal
from memes_platform.infrastructure.logging import configure_logging


class AdminCommand(StrEnum):
    """Воспроизводимые административные операции."""

    OPERATOR = "operator"
    CLEANUP = "cleanup"
    REPLAY_DELETIONS = "replay-deletions"
    SEED_SHADOW_DELETIONS = "seed-shadow-deletions"
    SHADOW_SEEDED = "identity_shadow_deletion_baseline_accepted"
    SHADOW_REQUIRED = "MEMES_SHADOW_SEARCH_URL is required"
    REPLAY_DONE = "identity_deletion_journal_replayed"
    PASSWORD_ENV = "MEMES_BOOTSTRAP_PASSWORD"  # noqa: S105
    OPERATOR_UPDATED = "identity_operator_updated"
    CLEANUP_DONE = "identity_expired_data_cleaned"


logger = logging.getLogger(__name__)


async def execute(
    settings: IdentitySettings,
    command: AdminCommand,
    *,
    login: str | None,
    password: str | None,
) -> None:
    """Выполнить одну административную операцию.

    Args:
        settings: Окружение релиза.
        command: Выбранная операция.
        login: Логин оператора.
        password: Исходный пароль для создания оператора.
    """
    configure_logging(level=settings.log_level, release_id=settings.release_id)
    engine = create_engine(settings.database_url)
    factory = session_factory(engine)
    try:
        if command is AdminCommand.OPERATOR:
            if login is None:
                raise ValueError("operator_login_required")
            operators = OperatorUseCases(
                uow=SqlIdentityUnitOfWork(factory, settings),
                passwords=Argon2PasswordVerifier(settings),
                clock=SystemClock(),
                policy=PasswordPolicy(),
            )
            await operators.bootstrap(login=login, password=password)
            logger.info(AdminCommand.OPERATOR_UPDATED)
        elif command is AdminCommand.REPLAY_DELETIONS:
            journal = S3DeletionJournal(settings)
            try:
                accounts = AccountUseCases(
                    uow=SqlIdentityUnitOfWork(factory, settings),
                    passwords=Argon2PasswordVerifier(settings),
                    clock=SystemClock(),
                    policy=PasswordPolicy(),
                    journal=journal,
                )
                await accounts.replay_deletion_journal()
            finally:
                journal.close()
            logger.info(AdminCommand.REPLAY_DONE)
        elif command is AdminCommand.SEED_SHADOW_DELETIONS:
            settings.require_outbox_configuration()
            if not settings.shadow_search_url:
                raise ValueError(AdminCommand.SHADOW_REQUIRED)
            async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as client:
                baseline = SeedShadowSearchDeletions(
                    uow=SqlIdentityUnitOfWork(factory, settings),
                    source=SqlDeletedOwnerSource(factory),
                    publisher=RestAccountDeletionPublisher(client, settings),
                    clock=SystemClock(),
                    destination=DeletionDestination(
                        recipient=DeletionRecipient.SHADOW_SEARCH,
                        base_url=settings.shadow_search_url,
                    ),
                )
                await baseline.execute()
            logger.info(AdminCommand.SHADOW_SEEDED)
        elif command is AdminCommand.CLEANUP:
            maintenance = IdentityMaintenanceUseCases(
                repository=SqlIdentityMaintenance(factory),
                clock=SystemClock(),
                login_window_seconds=settings.login_window_seconds,
            )
            await maintenance.cleanup()
            logger.info(AdminCommand.CLEANUP_DONE)
    finally:
        await engine.dispose()


def main() -> None:
    """Запустить admin process с теми же зависимостями и окружением."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            AdminCommand.OPERATOR,
            AdminCommand.CLEANUP,
            AdminCommand.REPLAY_DELETIONS,
            AdminCommand.SEED_SHADOW_DELETIONS,
        ],
    )
    parser.add_argument("--login")
    arguments = parser.parse_args()
    command = AdminCommand(arguments.command)
    password = environ.get(AdminCommand.PASSWORD_ENV)
    if command is AdminCommand.OPERATOR and password is None:
        password = getpass("Пароль для создания оператора: ")
    asyncio.run(execute(IdentitySettings(), command, login=arguments.login, password=password))


if __name__ == "__main__":
    main()
