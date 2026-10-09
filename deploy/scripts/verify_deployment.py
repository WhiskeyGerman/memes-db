"""Smoke проверка настоящих API после одноразовых миграций релиза."""

from enum import IntEnum, StrEnum
from http import HTTPStatus

import httpx


class ServicePort(IntEnum):
    """Локальные порты отдельного Compose стенда."""

    IDENTITY = 8080
    CATALOG = 8081
    PROCESSING = 8082
    SEARCH = 8083


class ProbeTimeout(IntEnum):
    """Ограничение обращения к каждому API."""

    SECONDS = 5


class ProbeSetting(StrEnum):
    """Безопасные публичные проверки без credentials."""

    HOST = "http://127.0.0.1"
    LIVE = "/health/live"
    READY = "/health/ready"
    METRICS = "/metrics"
    OPENAPI = "/openapi.json"


def main() -> None:
    """Остановить verification при недоступности любого обязательного API."""
    with httpx.Client(timeout=ProbeTimeout.SECONDS) as client:
        for port in ServicePort:
            for path in ProbeSetting:
                if path is ProbeSetting.HOST:
                    continue
                response = client.get(f"{ProbeSetting.HOST}:{port}{path}")
                if response.status_code != HTTPStatus.OK:
                    raise RuntimeError(f"{port.name} {path} {response.status_code}")
                if (
                    path is ProbeSetting.METRICS
                    and "memes_metrics_dependency_available" not in response.text
                ):
                    raise RuntimeError(f"{port.name} metric contract missing")


if __name__ == "__main__":
    main()
