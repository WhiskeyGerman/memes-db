import logging
from http import HTTPStatus
from sys import exc_info
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import StatementError

from memes_platform.infrastructure.logging import JsonFormatter, ServiceName
from memes_platform.presentation.observability import install_observability


@pytest.fixture
def sensitive_value() -> str:
    return uuid4().hex


@pytest.fixture
def json_formatter(sensitive_value: str) -> JsonFormatter:
    return JsonFormatter(
        "fixture-release",
        service_name=ServiceName.CATALOG,
        environment="test",
        secrets=(sensitive_value,),
    )


@pytest.fixture
def exception_record(sensitive_value: str) -> logging.LogRecord:
    try:
        raise ValueError(
            f"Fetch https://storage.invalid/bucket/cat.png failed: token={sensitive_value}"
        )
    except ValueError:
        return logging.LogRecord(
            "fixture", logging.ERROR, __file__, 0, "Операция завершилась ошибкой.", (), exc_info()
        )


async def completed_response() -> dict[str, str]:
    return {"status": "ok"}


async def rejected_response() -> None:
    raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY)


async def unavailable_response() -> None:
    raise HTTPException(status_code=HTTPStatus.SERVICE_UNAVAILABLE)


async def failed_response() -> None:
    raise RuntimeError("Resource https://storage.invalid/bucket/cat.png is unavailable")


@pytest.fixture
def chained_exception_record(sensitive_value: str) -> logging.LogRecord:
    try:
        try:
            raise ConnectionError(
                f"GET https://storage.invalid/bucket/cat.png?token={sensitive_value}&part=2 failed"
            )
        except ConnectionError as error:
            raise RuntimeError("Cannot build preview") from error
    except RuntimeError:
        return logging.LogRecord(
            "fixture", logging.ERROR, __file__, 0, "Preview failed", (), exc_info()
        )


class NumericInput(BaseModel):
    count: int


@pytest.fixture
def validation_exception_record(sensitive_value: str) -> logging.LogRecord:
    try:
        NumericInput.model_validate({"count": sensitive_value})
    except ValidationError:
        return logging.LogRecord(
            "fixture", logging.ERROR, __file__, 0, "Invalid input", (), exc_info()
        )
    raise AssertionError("The fixture must fail input validation")


@pytest.fixture
def statement_exception_record(sensitive_value: str) -> logging.LogRecord:
    error = StatementError(
        "Statement failed",
        "INSERT INTO credentials VALUES (:password)",
        {"password": sensitive_value},
        ConnectionError("Database db.internal:5432/memes_catalog is unavailable"),
    )
    return logging.LogRecord(
        "fixture", logging.ERROR, __file__, 0, "Database error", (), (type(error), error, None)
    )


@pytest.fixture
async def logging_client() -> httpx.AsyncClient:
    application = FastAPI()
    install_observability(application, service=ServiceName.CATALOG)
    application.add_api_route("/ok", completed_response)
    application.add_api_route("/rejected", rejected_response)
    application.add_api_route("/unavailable", unavailable_response)
    application.add_api_route("/failed", failed_response)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application, raise_app_exceptions=False),
        base_url="http://fixture",
    )
