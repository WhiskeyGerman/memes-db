"""Port binding и жизненный цикл stateless Search web-процесса."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from http import HTTPStatus
from typing import cast

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from memes_platform.infrastructure.logging import ServiceName, configure_logging
from memes_platform.infrastructure.metrics import PostgreSQLMetrics
from memes_platform.presentation.cors import install_cors
from memes_platform.presentation.observability import install_observability
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.infrastructure.configuration import SearchText, Settings
from memes_search.infrastructure.metrics import QUERIES
from memes_search.presentation.body_limit import BodyLimitMiddleware
from memes_search.presentation.container import Runtime, create_runtime
from memes_search.presentation.index_alias import IndexAliasMiddleware
from memes_search.presentation.routes import internal, public


class HealthStatus(StrEnum):
    """Публичная живость и готовность текущего процесса."""

    ALIVE = "alive"
    READY = "ready"
    UNAVAILABLE = "unavailable"


class LivenessResponse(BaseModel):
    """Ответ независимой проверки живости."""

    status: HealthStatus


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Создать внешние подключения и закрыть их при остановке."""
    settings = Settings()
    configure_logging(
        level=settings.log_level,
        release_id=settings.release_id,
        service_name=ServiceName.SEARCH,
        environment=settings.deployment_environment,
    )
    application.state.settings = settings
    runtime = create_runtime(settings)
    application.state.runtime = runtime
    application.state.observability.provider = PostgreSQLMetrics(runtime.engine, QUERIES)
    try:
        yield
    finally:
        await runtime.close()


def create_app() -> FastAPI:
    """Создать HTTP-приложение, не открывая подключения до запуска."""
    application = FastAPI(title=SearchText.TITLE, lifespan=lifespan)
    application.add_middleware(BodyLimitMiddleware)
    install_observability(application, service="search")
    install_cors(application)
    application.add_middleware(IndexAliasMiddleware)
    application.include_router(public)
    application.include_router(internal)
    application.add_api_route("/health/live", liveness, response_model=LivenessResponse)
    application.add_api_route("/health/ready", readiness)
    application.add_exception_handler(SearchError, search_error_handler)
    return application


def liveness() -> LivenessResponse:
    """Подтвердить живость независимо от доступности внешних ресурсов."""
    return LivenessResponse(status=HealthStatus.ALIVE)


async def readiness(request: Request) -> JSONResponse:
    """Проверить PostgreSQL и наличие применённых миграций Search."""
    runtime = cast(Runtime, request.app.state.runtime)
    ready = await runtime.ready()
    return JSONResponse(
        status_code=HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
        content={"status": HealthStatus.READY if ready else HealthStatus.UNAVAILABLE},
    )


async def search_error_handler(request: Request, error: Exception) -> JSONResponse:
    """Отобразить прикладной отказ в безопасный HTTP-ответ без данных запроса."""
    failure = cast(SearchError, error)
    statuses = {
        SearchErrorCode.NOT_FOUND: HTTPStatus.NOT_FOUND,
        SearchErrorCode.FORBIDDEN: HTTPStatus.FORBIDDEN,
        SearchErrorCode.SESSION_EXPIRED: HTTPStatus.GONE,
        SearchErrorCode.GROUPS_CHANGED: HTTPStatus.CONFLICT,
        SearchErrorCode.SAMPLE_TOO_LARGE: HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
        SearchErrorCode.SAMPLE_LIMIT: HTTPStatus.TOO_MANY_REQUESTS,
        SearchErrorCode.UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE,
        SearchErrorCode.INFERENCE_FAILED: HTTPStatus.SERVICE_UNAVAILABLE,
        SearchErrorCode.SAMPLE_FAILED: HTTPStatus.UNPROCESSABLE_ENTITY,
        SearchErrorCode.INVALID_SAMPLE: HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
    }
    return JSONResponse(
        status_code=statuses.get(failure.code, HTTPStatus.UNPROCESSABLE_ENTITY),
        content={"code": failure.code},
    )


def main() -> None:
    """Запустить самостоятельный HTTP-процесс по конфигурации окружения."""
    settings = Settings()
    configure_logging(
        level=settings.log_level,
        release_id=settings.release_id,
        service_name=ServiceName.SEARCH,
        environment=settings.deployment_environment,
    )
    uvicorn.run(
        SearchText.IMPORT_PATH,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=settings.shutdown_seconds,
    )


app = create_app()
