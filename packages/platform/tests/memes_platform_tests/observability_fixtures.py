from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError

from memes_platform.application.dto.metrics import MetricName, MetricSample
from memes_platform.application.ports.metrics import IMetricsProvider
from memes_platform.presentation.body_limit import RequestBodyLimitMiddleware
from memes_platform.presentation.observability import MetricsState, install_observability


@dataclass(slots=True)
class MetricsProviderFixture(IMetricsProvider):
    samples: tuple[MetricSample, ...]
    error: SQLAlchemyError | OSError | TimeoutError | None = None

    async def snapshot(self) -> tuple[MetricSample, ...]:
        if self.error is not None:
            raise self.error
        return self.samples


async def consume_payload(body: dict[str, str]) -> dict[str, str]:
    return body


@pytest.fixture
def observed_application() -> FastAPI:
    application = FastAPI()
    install_observability(application, service="fixture")
    application.add_middleware(RequestBodyLimitMiddleware)
    application.add_api_route("/consume", consume_payload, methods=["POST"])
    return application


@pytest.fixture
def metrics_state(observed_application: FastAPI) -> MetricsState:
    state: MetricsState = observed_application.state.observability
    return state


@pytest.fixture
def metrics_provider(metrics_state: MetricsState) -> MetricsProviderFixture:
    provider = MetricsProviderFixture(
        samples=(MetricSample(name=MetricName.USERS_ACTIVE, value=1),)
    )
    metrics_state.provider = provider
    return provider


@pytest.fixture(
    params=[SQLAlchemyError("database"), ConnectionRefusedError("network"), TimeoutError()]
)
def metrics_failure(request: pytest.FixtureRequest) -> SQLAlchemyError | OSError | TimeoutError:
    error: SQLAlchemyError | OSError | TimeoutError = request.param
    return error
