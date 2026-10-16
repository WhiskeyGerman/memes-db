import json
import logging
from datetime import UTC, datetime
from http import HTTPStatus
from uuid import uuid4

import httpx
import pytest

from memes_platform.infrastructure.correlation import request_id
from memes_platform.infrastructure.logging import JsonFormatter, LogField, LogText, LogUnit


def test_event_timestamp_is_creation_time_with_millisecond_precision(
    json_formatter: JsonFormatter,
) -> None:
    record = logging.LogRecord("fixture", logging.INFO, __file__, 0, "Готово.", (), None)
    record.created = datetime(2026, 10, 16, 7, 8, 9, 123000, tzinfo=UTC).timestamp()
    event = json.loads(json_formatter.format(record))
    assert event[LogField.TIME] == "2026-10-16T07:08:09.123+00:00"
    assert event[LogField.SERVICE] == "catalog"
    assert event[LogField.VERSION] == "fixture-release"
    assert event[LogField.ENVIRONMENT] == "test"
    assert event[LogField.SEVERITY] == LogUnit.INFO
    assert "trace_id" not in event


def test_secret_values_are_redacted_in_message_template_and_allowed_fields(
    json_formatter: JsonFormatter, sensitive_value: str
) -> None:
    record = logging.LogRecord(
        "fixture", logging.INFO, __file__, 0, "Запрос %s", (sensitive_value,), None
    )
    record.__dict__[LogField.TEMPLATE] = f"password={sensitive_value}"
    record.__dict__[LogField.ROUTE] = f"https://storage.invalid/file?token={sensitive_value}"
    record.__dict__["password"] = sensitive_value
    record.__dict__["headers"] = {"Authorization": sensitive_value}
    formatted = json_formatter.format(record)
    event = json.loads(formatted)
    assert sensitive_value not in formatted
    assert event[LogField.ROUTE] == "https://storage.invalid/file?token=[REDACTED]"
    assert "headers" not in event and "password" not in event
    assert LogText.REDACTED in event[LogField.MESSAGE]


@pytest.mark.parametrize(
    "message",
    ["password=do-not-record", 'token: "do-not-record"', "Bearer do-not-record"],
)
def test_credentials_are_redacted_without_an_environment_match(
    json_formatter: JsonFormatter, message: str
) -> None:
    record = logging.LogRecord("fixture", logging.INFO, __file__, 0, message, (), None)
    assert "do-not-record" not in json_formatter.format(record)


def test_exception_preserves_frames_without_payload_source_or_local_variables(
    json_formatter: JsonFormatter, exception_record: logging.LogRecord, sensitive_value: str
) -> None:
    formatted = json_formatter.format(exception_record)
    event = json.loads(formatted)
    assert event[LogField.ERROR_TYPE] == "ValueError"
    assert event[LogField.ERROR_MESSAGE] == (
        "Fetch https://storage.invalid/bucket/cat.png failed: token=[REDACTED]"
    )
    assert "logging_fixtures.py:" in event[LogField.ERROR_STACK]
    assert "memes_platform_tests.logging_fixtures.exception_record" in event[LogField.ERROR_STACK]
    assert sensitive_value not in formatted
    assert "raise ValueError" not in formatted
    assert LogField.SEVERITY in event


@pytest.mark.parametrize(
    ("path", "status", "level"),
    [
        ("/ok", HTTPStatus.OK, logging.INFO),
        ("/rejected", HTTPStatus.UNPROCESSABLE_ENTITY, logging.INFO),
        ("/unavailable", HTTPStatus.SERVICE_UNAVAILABLE, logging.ERROR),
        ("/failed", HTTPStatus.INTERNAL_SERVER_ERROR, logging.ERROR),
    ],
)
async def test_http_events_keep_context_duration_and_correct_severity(
    logging_client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
    json_formatter: JsonFormatter,
    path: str,
    status: HTTPStatus,
    level: int,
) -> None:
    caplog.set_level(logging.INFO)
    identifier = str(uuid4())
    async with logging_client:
        response = await logging_client.get(path, headers={"X-Request-ID": identifier})
    assert response.status_code == status
    records = [
        record
        for record in caplog.records
        if record.__dict__.get(LogField.EVENT) == LogText.HTTP_EVENT
    ]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == level
    assert record.__dict__[LogField.STATUS] == status
    assert record.__dict__[LogField.REQUEST] == identifier
    if path != "/failed":
        assert response.headers["X-Request-ID"] == identifier
    assert record.__dict__[LogField.DURATION_MS] >= 0
    assert record.__dict__[LogField.DURATION_MS] == pytest.approx(
        record.__dict__[LogField.DURATION_SECONDS] * LogUnit.MILLISECONDS_PER_SECOND
    )
    assert request_id.get() is None
    formatted = json_formatter.format(record)
    if path == "/failed":
        assert json.loads(formatted)[LogField.ERROR_TYPE] == "RuntimeError"
        assert json.loads(formatted)[LogField.ERROR_MESSAGE] == (
            "Resource https://storage.invalid/bucket/cat.png is unavailable"
        )


def test_exception_chain_keeps_causes_resource_and_module_names(
    json_formatter: JsonFormatter, chained_exception_record: logging.LogRecord, sensitive_value: str
) -> None:
    event = json.loads(json_formatter.format(chained_exception_record))
    assert event[LogField.ERROR_MESSAGE] == "Cannot build preview"
    stack = event[LogField.ERROR_STACK]
    assert "ConnectionError" in stack and "RuntimeError" in stack
    assert "https://storage.invalid/bucket/cat.png?token=[REDACTED]&part=2" in stack
    assert "memes_platform_tests.logging_fixtures.chained_exception_record" in stack
    assert sensitive_value not in stack
    assert "raise ConnectionError" not in stack


def test_validation_error_keeps_reason_without_serializing_input(
    json_formatter: JsonFormatter,
    validation_exception_record: logging.LogRecord,
    sensitive_value: str,
) -> None:
    formatted = json_formatter.format(validation_exception_record)
    assert sensitive_value not in formatted
    message = json.loads(formatted)[LogField.ERROR_MESSAGE]
    assert "count" in message and "int_parsing" in message
    assert "input" not in json.loads(message)[0]


def test_statement_error_keeps_driver_reason_without_sql_or_parameters(
    json_formatter: JsonFormatter,
    statement_exception_record: logging.LogRecord,
    sensitive_value: str,
) -> None:
    formatted = json_formatter.format(statement_exception_record)
    assert sensitive_value not in formatted
    assert "INSERT INTO" not in formatted
    assert json.loads(formatted)[LogField.ERROR_MESSAGE] == (
        "Database db.internal:5432/memes_catalog is unavailable"
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_invalid_numeric_extra_does_not_destroy_the_log_record(
    json_formatter: JsonFormatter, value: float
) -> None:
    record = logging.LogRecord("fixture", logging.INFO, __file__, 0, "Request completed", (), None)
    record.__dict__[LogField.DURATION_MS] = value
    event = json.loads(json_formatter.format(record))
    assert event[LogField.MESSAGE] == "Request completed"
    assert LogField.DURATION_MS not in event


def test_bad_message_format_does_not_trigger_unsanitized_logging_fallback(
    json_formatter: JsonFormatter, sensitive_value: str
) -> None:
    record = logging.LogRecord(
        "fixture", logging.ERROR, __file__, 0, "Failed %d", (sensitive_value,), None
    )
    event = json.loads(json_formatter.format(record))
    assert event[LogField.MESSAGE] == "Failed %d"
    assert event[LogText.FORMAT_ERROR] == "TypeError"


async def test_success_inside_parent_exception_handler_is_not_reported_as_failure(
    logging_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    try:
        raise ValueError("Unrelated parent exception")
    except ValueError:
        async with logging_client:
            response = await logging_client.get("/ok")
    assert response.status_code == HTTPStatus.OK
    record = next(record for record in caplog.records if LogField.EVENT in record.__dict__)
    assert record.levelno == logging.INFO
    assert not record.exc_info


async def test_actual_resource_path_is_logged_without_query_or_metric_cardinality(
    logging_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    resource = f"/v1/media/{uuid4()}"
    async with logging_client:
        response = await logging_client.get(resource + "?token=must-not-be-logged")
    assert response.status_code == HTTPStatus.NOT_FOUND
    record = next(record for record in caplog.records if LogField.EVENT in record.__dict__)
    assert record.__dict__[LogField.URL_PATH] == resource
    assert record.__dict__[LogField.ROUTE] == "unmatched"
    assert "must-not-be-logged" not in str(record.__dict__)
