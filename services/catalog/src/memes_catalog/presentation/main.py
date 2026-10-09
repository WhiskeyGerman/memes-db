"""Composition root stateless HTTP-процесса Catalog."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from http import HTTPStatus

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SQLTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine

from memes_catalog.application.use_cases.catalog import CatalogUseCases
from memes_catalog.application.use_cases.variations import VariationUseCases
from memes_catalog.infrastructure.clock import SystemClock
from memes_catalog.infrastructure.configuration import CatalogSetting, CatalogSettings
from memes_catalog.infrastructure.http.clients import IdentityClient, RestDelivery
from memes_catalog.infrastructure.media.inspector import MediaInspector
from memes_catalog.infrastructure.metrics import QUERIES
from memes_catalog.infrastructure.persistence.catalog import PostgresCatalogFactory
from memes_catalog.infrastructure.storage.s3 import S3ObjectStorage
from memes_catalog.presentation.api import internal, public
from memes_catalog.presentation.dependencies import CatalogContainer, container
from memes_platform.application.errors import ApplicationError, ErrorCode
from memes_platform.infrastructure.database import create_engine, session_factory
from memes_platform.infrastructure.deletion_journal import S3DeletionJournal
from memes_platform.infrastructure.http import InternalHttpClient
from memes_platform.infrastructure.logging import configure_logging
from memes_platform.infrastructure.metrics import PostgreSQLMetrics
from memes_platform.presentation.body_limit import RequestBodyLimitMiddleware
from memes_platform.presentation.cors import install_cors
from memes_platform.presentation.errors import application_error_handler, install_error_handlers
from memes_platform.presentation.observability import install_observability


class HealthValue(StrEnum):
    """Публичные сигналы жизненного цикла процесса."""

    LIVE_PATH = "/health/live"
    READY_PATH = "/health/ready"
    ALIVE = "alive"
    READY = "ready"


class HealthResponse(BaseModel):
    """Ответ без конфигурации и секретов."""

    status: str


def build_container(settings: CatalogSettings, engine: AsyncEngine) -> CatalogContainer:
    """Связать конкретные ресурсы с прикладными портами.

    Args:
        settings: Конфигурация.
        engine: Пул PostgreSQL.
    """
    factory = PostgresCatalogFactory(
        engine, session_factory(engine), mirror_search=settings.shadow_search_url is not None
    )
    storage = S3ObjectStorage(settings, public_endpoint=settings.s3_public_endpoint_url)
    identity = IdentityClient(
        InternalHttpClient(
            base_url=settings.identity_url,
            internal_api_key=settings.internal_api_key,
            timeout=settings.http_timeout_seconds,
        )
    )
    delivery = RestDelivery(settings)
    policy = settings.media_policy()
    catalog = CatalogUseCases(
        factory,
        storage,
        MediaInspector(
            storage,
            policy,
            ffprobe=settings.ffprobe_path,
            concurrency=settings.inspection_concurrency,
            timeout=settings.inspection_timeout_seconds,
        ),
        identity,
        delivery,
        policy=policy,
        clock=SystemClock(),
        journal=S3DeletionJournal(settings),
        concurrent_limit=settings.concurrent_uploads,
        daily_limit=settings.daily_publications,
        storage_limit=settings.storage_capacity_bytes,
        free_percent=settings.free_storage_percent,
        queue_limit=settings.processing_queue_limit,
        upload_seconds=settings.upload_seconds,
        claim_seconds=settings.claim_seconds,
    )
    variations = VariationUseCases(
        factory,
        merge_threshold=settings.merge_threshold,
        detach_threshold=settings.detach_threshold,
        policy_revision=settings.voting_policy_revision,
        vote_rate=settings.vote_rate_per_minute,
    )
    return CatalogContainer(
        settings=settings,
        factory=factory,
        storage=storage,
        identity=identity,
        delivery=delivery,
        catalog=catalog,
        variations=variations,
    )


async def close_container(dependencies: CatalogContainer) -> None:
    """Освободить принадлежащие процессу транспортные ресурсы.

    Args:
        dependencies: Контейнер.
    """
    await dependencies.identity.client.close()
    await dependencies.delivery.close()
    dependencies.storage.client.close()
    dependencies.storage.signer.close()
    if isinstance(dependencies.catalog.journal, S3DeletionJournal):
        dependencies.catalog.journal.close()


async def database_error(request: Request, error: Exception) -> JSONResponse:
    """Скрыть SQL и параметры известной ошибки PostgreSQL.

    Args:
        request: Внешний запрос.
        error: Ошибка DBAPI или ожидания пула.
    """
    return await application_error_handler(request, ApplicationError(ErrorCode.UNAVAILABLE))


def create_app(settings: CatalogSettings | None = None) -> FastAPI:
    """Создать приложение без импорта ресурсов развёртывания.

    Args:
        settings: Явная конфигурация либо окружение при старте.
    """

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """Создать ресурсы и гарантировать graceful shutdown."""
        configuration = settings or CatalogSettings()
        configure_logging(level=configuration.log_level, release_id=configuration.release_id)
        engine = create_engine(
            configuration.database_url,
            pool_size=configuration.database_pool_size,
            max_overflow=configuration.database_pool_overflow,
        )
        dependencies = build_container(configuration, engine)
        application.state.catalog = dependencies
        application.state.internal_api_key = configuration.internal_api_key
        application.state.observability.provider = PostgreSQLMetrics(engine, QUERIES)
        try:
            yield
        finally:
            await close_container(dependencies)
            await engine.dispose()

    application = FastAPI(title=CatalogSetting.TITLE, lifespan=lifespan)
    application.add_middleware(RequestBodyLimitMiddleware)
    install_observability(application, service="catalog")
    install_cors(application)
    application.include_router(public.router)
    application.include_router(internal.router)
    application.add_api_route(HealthValue.LIVE_PATH, liveness, status_code=HTTPStatus.OK)
    application.add_api_route(HealthValue.READY_PATH, readiness, status_code=HTTPStatus.OK)
    install_error_handlers(application)
    application.add_exception_handler(DBAPIError, database_error)
    application.add_exception_handler(SQLTimeoutError, database_error)
    return application


def liveness() -> HealthResponse:
    """Подтвердить живость без подключаемых ресурсов."""
    return HealthResponse(status=HealthValue.ALIVE)


async def readiness(request: Request) -> HealthResponse:
    """Проверить обязательные PostgreSQL и S3.

    Args:
        request: Запрос.
    """
    dependencies = container(request)
    journal = dependencies.catalog.journal
    if (
        not await dependencies.factory.ready()
        or not await dependencies.storage.ready()
        or (isinstance(journal, S3DeletionJournal) and not await journal.ready())
    ):
        raise ApplicationError(ErrorCode.UNAVAILABLE)
    return HealthResponse(status=HealthValue.READY)


def main() -> None:
    """Запустить HTTP-сервер с конфигурацией окружения."""
    settings = CatalogSettings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        timeout_graceful_shutdown=settings.shutdown_seconds,
        log_config=None,
        access_log=False,
    )


app = create_app()
