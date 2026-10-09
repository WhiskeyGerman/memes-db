"""Структурированный поток логов без секретов и локальных файлов."""

import json
import logging
import sys
from datetime import UTC, datetime
from enum import StrEnum

from memes_platform.infrastructure.correlation import request_id


class LogField(StrEnum):
    """Имена стандартных полей структурированного события."""

    TIME = "timestamp"
    LEVEL = "level"
    LOGGER = "logger"
    MESSAGE = "message"
    RELEASE = "release_id"


class JsonFormatter(logging.Formatter):
    """Преобразование события в одну JSON-строку."""

    def __init__(self, release_id: str) -> None:
        """Сохранить идентификатор релиза.

        Args:
            release_id: Идентификатор неизменяемого артефакта.
        """
        super().__init__()
        self.release_id = release_id

    def format(self, record: logging.LogRecord) -> str:
        """Сформировать безопасное стандартное событие.

        Args:
            record: Запись logger.
        """
        event: dict[str, str | int | float] = {
            LogField.TIME: datetime.now(UTC).isoformat(),
            LogField.LEVEL: record.levelname,
            LogField.LOGGER: record.name,
            LogField.MESSAGE: record.getMessage(),
            LogField.RELEASE: self.release_id,
        }
        if (identifier := request_id.get()) is not None:
            event["request_id"] = identifier
        for key in (
            "media_id",
            "task_id",
            "attempt_id",
            "worker_id",
            "method",
            "route",
            "status",
            "duration_seconds",
        ):
            if key in record.__dict__:
                event[key] = record.__dict__[key]
        return json.dumps(event, ensure_ascii=False)


def configure_logging(*, level: str, release_id: str) -> None:
    """Настроить stdout поток процесса.

    Args:
        level: Минимальный уровень.
        release_id: Идентификатор релиза.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(release_id))
    logging.basicConfig(level=level, handlers=[handler], force=True)

    for name in ("httpx", "httpcore", "botocore", "boto3", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
