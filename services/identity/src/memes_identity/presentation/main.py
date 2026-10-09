"""HTTP composition root; состояние хранится во внешнем PostgreSQL."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from http import HTTPStatus

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel
from sqlalchemy import literal, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SqlTimeoutError

from memes_identity.application.errors import IdentityError
from memes_identity.infrastructure.authentication.passwords import Argon2PasswordVerifier
from memes_identity.infrastructure.clock import SystemClock
from memes_identity.infrastructure.configuration import IdentitySettings
from memes_identity.infrastructure.metrics import QUERIES
from memes_identity.presentation.api import accounts, authentication, internal
from memes_identity.presentation.api.errors import (
    database_error,
    domain_error,
    identity_error,
    validation_error,
)
from memes_identity.presentation.dependencies import IdentityContainer, container
from memes_kernel.errors import DomainError
from memes_kernel.values import Number
from memes_platform.application.errors import ApplicationError
from memes_platform.infrastructure.database import create_engine, session_factory
from memes_platform.infrastructure.deletion_journal import S3DeletionJournal
from memes_platform.infrastructure.logging import configure_logging
from memes_platform.infrastructure.metrics import PostgreSQLMetrics
from memes_platform.presentation.body_limit import RequestBodyLimitMiddleware
from memes_platform.presentation.cors import install_cors
from memes_platform.presentation.errors import application_error_handler
from memes_platform.presentation.observability import install_observability


class ServiceSetting(StrEnum):
    """Постоянные маршруты и название приложения."""

    TITLE = "Memes identity"
    HEALTH_PATH = "/health/live"
    READY_PATH = "/health/ready"
    ALIVE = "alive"
    READY = "ready"


class LivenessResponse(BaseModel):
    """Состояние процесса без чувствительных данных."""

    status: str


def create_app(settings: IdentitySettings | None = None) -> FastAPI:
    """Создать приложение без подключения к БД во время импорта.

    Args:
        settings: Явная конфигурация либо чтение окружения при старте.

    Returns:
        FastAPI: HTTP-процесс с управляемым жизненным циклом ресурсов.
    """

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """Создать и освободить подключения текущего процесса.

        Args:
            application: Запускаемое приложение.
        """
        configuration = settings or IdentitySettings()
        configuration.require_http_configuration()
        configuration.require_outbox_configuration()
        configure_logging(level=configuration.log_level, release_id=configuration.release_id)
        engine = create_engine(
            configuration.database_url,
            pool_size=configuration.database_pool_size,
            max_overflow=configuration.database_pool_overflow,
        )
        journal = S3DeletionJournal(configuration)
        application.state.identity = IdentityContainer(
            settings=configuration,
            factory=session_factory(engine),
            passwords=Argon2PasswordVerifier(configuration),
            clock=SystemClock(),
            journal=journal,
        )
        application.state.internal_api_key = configuration.internal_api_key
        application.state.observability.provider = PostgreSQLMetrics(engine, QUERIES)
        try:
            yield
        finally:
            journal.close()
            await engine.dispose()

    application = FastAPI(title=ServiceSetting.TITLE, lifespan=lifespan)
    application.add_middleware(RequestBodyLimitMiddleware)
    install_observability(application, service="identity")
    install_cors(application)
    application.include_router(authentication.router)
    application.include_router(accounts.router)
    application.include_router(internal.router)
    application.add_api_route(ServiceSetting.HEALTH_PATH, liveness, status_code=HTTPStatus.OK)
    application.add_api_route(
        ServiceSetting.READY_PATH,
        readiness,
        dependencies=[Depends(container)],
        status_code=HTTPStatus.OK,
    )
    application.add_exception_handler(RequestValidationError, validation_error)
    application.add_exception_handler(ApplicationError, application_error_handler)
    application.add_exception_handler(IdentityError, identity_error)
    application.add_exception_handler(DomainError, domain_error)
    application.add_exception_handler(DBAPIError, database_error)
    application.add_exception_handler(SqlTimeoutError, database_error)
    return application


def liveness() -> LivenessResponse:
    """Подтвердить живость процесса без обращения к подключаемым ресурсам."""
    return LivenessResponse(status=ServiceSetting.ALIVE)


async def readiness(request: Request) -> LivenessResponse:
    """Проверить доступность внешнего PostgreSQL.

    Args:
        request: HTTP-запрос проверки готовности.
    """
    dependencies = container(request)
    async with dependencies.factory() as session:
        await session.execute(select(literal(int(Number.ONE))))
    return LivenessResponse(status=ServiceSetting.READY)


def main() -> None:
    """Запустить HTTP-сервер с портом и адресом из окружения релиза."""
    settings = IdentitySettings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        timeout_graceful_shutdown=settings.shutdown_seconds,
        log_config=None,
        access_log=False,
    )


app = create_app()
