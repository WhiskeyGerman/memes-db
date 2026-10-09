"""HTTP-метрики и корреляция на внешней границе ASGI."""

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from http import HTTPMethod, HTTPStatus
from time import perf_counter
from typing import cast
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
        context = request_id.set(identifier)
        started = perf_counter()
        status = HTTPStatus.INTERNAL_SERVER_ERROR

        async def observed_send(message: Message) -> None:
            """Дополнить только response start безопасным заголовком."""
            nonlocal status
            if message["type"] == "http.response.start":
                status = HTTPStatus(message["status"])
                MutableHeaders(scope=message)[MetricSetting.REQUEST_HEADER] = identifier
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        finally:
            duration = perf_counter() - started
            route = getattr(scope.get("route"), "path", MetricSetting.UNMATCHED)
            method = (
                request.method if request.method in KNOWN_METHODS else MetricSetting.UNKNOWN_METHOD
            )
            self.state.requests.labels(self.state.service, method, route, str(status.value)).inc()
            self.state.duration.labels(self.state.service, method, route).observe(duration)
            logger.info(
                "http_request_completed",
                extra={
                    "method": method,
                    "route": route,
                    "status": status.value,
                    "duration_seconds": duration,
                },
            )
            request_id.reset(context)


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
            logger.warning("metrics_dependency_unavailable")
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
