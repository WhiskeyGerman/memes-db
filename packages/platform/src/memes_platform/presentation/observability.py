"""HTTP-метрики и корреляция на внешней границе ASGI."""

import logging
from contextvars import Token
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPMethod, HTTPStatus
from time import perf_counter
from types import TracebackType
from typing import Self, cast
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import MutableHeaders
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from memes_platform.application.dto.metrics import MetricName
from memes_platform.application.ports.metrics import IMetricsProvider
from memes_platform.infrastructure.correlation import request_id
from memes_platform.infrastructure.logging import LogField, LogText, LogUnit


class MetricSetting(StrEnum):
    """Предопределённые имена с ограниченной кардинальностью."""

    HTTP = "http"
    REQUEST_HEADER = "X-Request-ID"
    ENDPOINT = "/metrics"
    UNMATCHED = "unmatched"
    UNKNOWN_METHOD = "OTHER"
    REQUESTS = "memes_http_requests_total"
    DURATION = "memes_http_request_duration_seconds"
    SCRAPE_OK = "memes_metrics_dependency_available"


logger = logging.getLogger(__name__)
KNOWN_METHODS = frozenset(method.value for method in HTTPMethod)


@dataclass(slots=True)
class MetricsState:
    """Registry одного приложения без глобальных и повторных регистраций."""

    service: str
    registry: CollectorRegistry = field(default_factory=CollectorRegistry)
    provider: IMetricsProvider | None = None
    requests: Counter = field(init=False)
    duration: Histogram = field(init=False)
    dependency: Gauge = field(init=False)
    gauges: dict[MetricName, Gauge] = field(init=False)

    def __post_init__(self) -> None:
        """Создать фиксированные метрики без ID пользователей и объектов."""
        self.requests = Counter(
            MetricSetting.REQUESTS,
            "HTTP requests",
            ["service", "method", "route", "status"],
            registry=self.registry,
        )
        self.duration = Histogram(
            MetricSetting.DURATION,
            "HTTP duration seconds",
            ["service", "method", "route"],
            registry=self.registry,
        )
        self.dependency = Gauge(
            MetricSetting.SCRAPE_OK,
            "Metrics backing resource available",
            ["service"],
            registry=self.registry,
        )
        self.gauges = {
            name: Gauge(name, name.value, ["service"], registry=self.registry)
            for name in MetricName
        }


@dataclass(slots=True)
class RequestObservation:
    """Завершить наблюдение и очистить контекст при любом исходе ASGI-вызова."""

    state: MetricsState
    scope: Scope
    method: str
    path: str
    identifier: str
    status: int = HTTPStatus.INTERNAL_SERVER_ERROR
    started: float = field(default_factory=perf_counter)
    token: Token[str | None] | None = field(default=None, init=False)

    def __enter__(self) -> Self:
        """Установить контекст запроса без изменения родительского контекста."""
        self.token = request_id.set(self.identifier)
        return self

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Записать фактический исход; исключение продолжает распространяться.

        Args:
            error_type: Тип исключения именно наблюдаемого вызова.
            error: Неперехваченное исключение, если вызов завершился неуспешно.
            traceback: Стек исходного исключения.
        """
        try:
            duration = perf_counter() - self.started
            route = getattr(self.scope.get("route"), "path", MetricSetting.UNMATCHED)
            self.state.requests.labels(
                self.state.service, self.method, route, str(self.status)
            ).inc()
            self.state.duration.labels(self.state.service, self.method, route).observe(duration)
            failed = error is not None or self.status >= HTTPStatus.INTERNAL_SERVER_ERROR
            logger.log(
                logging.ERROR if failed else logging.INFO,
                LogText.HTTP_MESSAGE,
                exc_info=(type(error), error, traceback) if error is not None else None,
                extra={
                    LogField.REQUEST: self.identifier,
                    LogField.EVENT: LogText.HTTP_EVENT,
                    LogField.TEMPLATE: LogText.HTTP_MESSAGE,
                    LogField.METHOD: self.method,
                    LogField.ROUTE: route,
                    LogField.URL_PATH: self.path,
                    LogField.STATUS: self.status,
                    LogField.DURATION_SECONDS: duration,
                    LogField.DURATION_MS: duration * LogUnit.MILLISECONDS_PER_SECOND,
                },
            )
        finally:
            if self.token is not None:
                request_id.reset(self.token)


class ObservabilityMiddleware:
    """Измерить завершённый запрос и передать только валидный UUID корреляции."""

    def __init__(self, app: ASGIApp, state: MetricsState) -> None:
        """Сохранить приложение и его registry.

        Args:
            app: ASGI-приложение.
            state: Наблюдаемое состояние процесса.
        """
        self.app = app
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Измерить только HTTP с ограниченными route template labels."""
        if scope["type"] != MetricSetting.HTTP:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        raw_identifier = request.headers.get(MetricSetting.REQUEST_HEADER)
        try:
            identifier = str(UUID(raw_identifier)) if raw_identifier else str(uuid4())
        except ValueError:
            identifier = str(uuid4())
        observation = RequestObservation(
            state=self.state,
            scope=scope,
            method=request.method
            if request.method in KNOWN_METHODS
            else MetricSetting.UNKNOWN_METHOD,
            path=request.url.path,
            identifier=identifier,
        )

        async def observed_send(message: Message) -> None:
            """Дополнить только response start безопасным заголовком."""
            if message["type"] == "http.response.start":
                observation.status = message["status"]
                MutableHeaders(scope=message)[MetricSetting.REQUEST_HEADER] = identifier
            await send(message)

        with observation:
            await self.app(scope, receive, observed_send)


async def metrics(request: Request) -> Response:
    """Экспортировать агрегаты без секретов; доступ ограничивается сетью ingress."""
    state = cast(MetricsState, request.app.state.observability)
    if state.provider is not None:
        try:
            for sample in await state.provider.snapshot():
                state.gauges[sample.name].labels(state.service).set(sample.value)
            state.dependency.labels(state.service).set(1)
        except SQLAlchemyError:
            state.dependency.labels(state.service).set(0)
            logger.warning("metrics_dependency_unavailable", exc_info=True)
            return Response(
                generate_latest(state.registry),
                media_type=CONTENT_TYPE_LATEST,
                status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            )
    return Response(generate_latest(state.registry), media_type=CONTENT_TYPE_LATEST)


def install_observability(application: FastAPI, *, service: str) -> MetricsState:
    """Подключить общий ASGI adapter в composition root.

    Args:
        application: Собранный HTTP-процесс.
        service: Предопределённое название владельца метрик.
    """
    state = MetricsState(service=service)
    application.state.observability = state
    application.add_middleware(ObservabilityMiddleware, state=state)
    application.add_api_route(MetricSetting.ENDPOINT, metrics, include_in_schema=False)
    return state
