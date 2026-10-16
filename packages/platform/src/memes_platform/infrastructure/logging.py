"""Структурированный stdout с контекстом процесса и защитой чувствительных данных."""

import json
import logging
import sys
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from math import isfinite
from os import environ
from pathlib import Path
from traceback import walk_tb

from pydantic import ValidationError
from sqlalchemy.exc import StatementError

from memes_platform.infrastructure.correlation import request_id
from memes_platform.infrastructure.log_sanitization import (
    LogSanitizer,
    RedactionText,
    environment_secrets,
)


class ServiceName(StrEnum):
    """Стабильные имена независимо запускаемых процессов."""

    IDENTITY = "identity"
    IDENTITY_OUTBOX = "identity-outbox"
    CATALOG = "catalog"
    CATALOG_MAINTENANCE = "catalog-maintenance"
    PROCESSING = "processing"
    PROCESSING_COORDINATOR = "processing-coordinator"
    SEARCH = "search"
    SEARCH_MAINTENANCE = "search-maintenance"


class LogField(StrEnum):
    """Имена разрешённых полей структурированного события."""

    TIME = "timestamp"
    LEVEL = "level"
    SEVERITY = "severity_number"
    LOGGER = "logger"
    MESSAGE = "message"
    TEMPLATE = "msg_template"
    EVENT = "event.name"
    RELEASE = "release_id"
    SERVICE = "service.name"
    VERSION = "service.version"
    ENVIRONMENT = "deployment.environment.name"
    REQUEST = "request_id"
    MEDIA = "media_id"
    TASK = "task_id"
    ATTEMPT = "attempt_id"
    WORKER = "worker_id"
    METHOD = "method"
    ROUTE = "route"
    URL = "url.full"
    URL_PATH = "url.path"
    EVENT_ID = "event_id"
    RETRIED_DELIVERIES = "retried_deliveries"
    RETRIED_CLEANUPS = "retried_cleanups"
    STATUS = "status"
    DURATION_SECONDS = "duration_seconds"
    DURATION_MS = "duration_ms"
    ERROR_TYPE = "error.type"
    ERROR_MESSAGE = "error.message"
    ERROR_STACK = "error.stack"


class LogText(StrEnum):
    """Фиксированные сообщения и правила редактирования."""

    MILLISECONDS = "milliseconds"
    REDACTED = RedactionText.REDACTED
    UNKNOWN_MODULE = "unknown"
    CAUSE = "Причина следующего исключения:"
    CONTEXT = "При обработке предыдущего исключения:"
    CYCLE = "[Циклическая ссылка на исключение]"
    FORMAT_ERROR = "log.format_error"
    HTTP_EVENT = "http_request_completed"
    HTTP_MESSAGE = "HTTP-запрос завершён."


class LogUnit(IntEnum):
    """Единицы измерения и номера уровней OpenTelemetry."""

    MILLISECONDS_PER_SECOND = 1000
    DEBUG = 5
    INFO = 9
    WARNING = 13
    ERROR = 17
    CRITICAL = 21


EXTRA_FIELDS = (
    LogField.REQUEST,
    LogField.TEMPLATE,
    LogField.EVENT,
    LogField.MEDIA,
    LogField.TASK,
    LogField.ATTEMPT,
    LogField.WORKER,
    LogField.METHOD,
    LogField.ROUTE,
    LogField.URL,
    LogField.URL_PATH,
    LogField.EVENT_ID,
    LogField.RETRIED_DELIVERIES,
    LogField.RETRIED_CLEANUPS,
    LogField.STATUS,
    LogField.DURATION_SECONDS,
    LogField.DURATION_MS,
)
SEVERITY_NUMBERS = {
    logging.DEBUG: LogUnit.DEBUG,
    logging.INFO: LogUnit.INFO,
    logging.WARNING: LogUnit.WARNING,
    logging.ERROR: LogUnit.ERROR,
    logging.CRITICAL: LogUnit.CRITICAL,
}


class JsonFormatter(logging.Formatter):
    """Сериализовать разрешённые поля; исключить payload, заголовки и SQL."""

    def __init__(
        self,
        release_id: str,
        *,
        service_name: ServiceName,
        environment: str,
        secrets: tuple[str, ...] = (),
    ) -> None:
        """Сохранить неизменяемый контекст процесса.

        Args:
            release_id: Версия запущенного артефакта.
            service_name: Стабильное имя процесса.
            environment: Имя окружения развёртывания.
            secrets: Значения секретов для дополнительной редакции сообщений.
        """
        super().__init__()
        self.release_id = release_id
        self.service_name = service_name
        self.environment = environment
        self.sanitizer = LogSanitizer(secrets)

    def redact(self, value: str) -> str:
        """Удалить credentials, сохранив URL ресурса и диагностический текст.

        Args:
            value: Текст разрешённого поля.

        Returns:
            str: Отредактированный текст без известных секретов.
        """
        return self.sanitizer.redact(value)

    def exception_message(self, error: BaseException) -> str:
        """Сохранить причину отказа без SQL-параметров и входных данных валидации.

        Args:
            error: Исходное исключение.

        Returns:
            str: Причина ошибки с точечной редакцией credentials.
        """
        if isinstance(error, StatementError) and error.orig is not None:
            message = str(error.orig)
        elif isinstance(error, ValidationError):
            message = error.json(include_input=False, include_context=False, include_url=False)
        else:
            message = str(error)
        return self.redact(message)

    def exception_stack(self, error: BaseException, seen: set[int] | None = None) -> str:
        """Сохранить цепочку причин и однозначные кадры без исходного кода и locals.

        Args:
            error: Исключение запроса или фоновой операции.
            seen: Уже посещённые исключения для защиты от циклов.

        Returns:
            str: Диагностический стек с отредактированными текстами причин.
        """
        visited = set() if seen is None else seen
        if id(error) in visited:
            return LogText.CYCLE
        visited.add(id(error))
        lines: list[str] = []
        if error.__cause__ is not None:
            lines.extend((self.exception_stack(error.__cause__, visited), LogText.CAUSE))
        elif error.__context__ is not None and not error.__suppress_context__:
            lines.extend((self.exception_stack(error.__context__, visited), LogText.CONTEXT))
        lines.extend(
            f"{frame.f_globals.get('__name__', LogText.UNKNOWN_MODULE)}."
            f"{frame.f_code.co_qualname} ({Path(frame.f_code.co_filename).name}:{line})"
            for frame, line in walk_tb(error.__traceback__)
        )
        lines.append(f"{type(error).__qualname__}: {self.exception_message(error)}")
        if isinstance(error, BaseExceptionGroup):
            lines.extend(self.exception_stack(child, visited) for child in error.exceptions)
        return self.redact("\n".join(lines))

    def format(self, record: logging.LogRecord) -> str:
        """Сформировать одну JSON-строку с временем создания события.

        Args:
            record: Запись стандартного :mod:`logging`.

        Returns:
            str: Событие без произвольных ``extra``, исходного кода и locals.
        """
        event: dict[str, str | int | float] = {
            LogField.TIME: datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec=LogText.MILLISECONDS
            ),
            LogField.LEVEL: record.levelname,
            LogField.SEVERITY: SEVERITY_NUMBERS.get(record.levelno, LogUnit.INFO),
            LogField.LOGGER: record.name,
            LogField.TEMPLATE: self.redact(str(record.msg)),
            LogField.RELEASE: self.redact(self.release_id),
            LogField.SERVICE: self.service_name,
            LogField.VERSION: self.redact(self.release_id),
            LogField.ENVIRONMENT: self.redact(self.environment),
        }
        try:
            event[LogField.MESSAGE] = self.redact(record.getMessage())
        except (TypeError, ValueError) as error:
            event[LogField.MESSAGE] = self.redact(str(record.msg))
            event[LogText.FORMAT_ERROR] = type(error).__name__
        if (identifier := request_id.get()) is not None:
            event[LogField.REQUEST] = identifier
        for key in EXTRA_FIELDS:
            value = record.__dict__.get(key)
            if isinstance(value, str):
                event[key] = self.redact(value)
            elif isinstance(value, int) or (isinstance(value, float) and isfinite(value)):
                event[key] = value
        if record.exc_info and record.exc_info[1] is not None:
            event[LogField.ERROR_TYPE] = type(record.exc_info[1]).__name__
            event[LogField.ERROR_MESSAGE] = self.exception_message(record.exc_info[1])
            event[LogField.ERROR_STACK] = self.exception_stack(record.exc_info[1])
        return json.dumps(event, ensure_ascii=False, allow_nan=False)


def configure_logging(
    *, level: str, release_id: str, service_name: ServiceName, environment: str
) -> None:
    """Настроить JSON в stdout и редакцию секретов из окружения.

    Args:
        level: Минимальный уровень событий.
        release_id: Версия запущенного артефакта.
        service_name: Стабильное имя процесса.
        environment: Имя окружения развёртывания.
    """
    secrets = environment_secrets(environ)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter(
            release_id, service_name=service_name, environment=environment, secrets=secrets
        )
    )
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for name in ("httpx", "httpcore", "botocore", "boto3", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
