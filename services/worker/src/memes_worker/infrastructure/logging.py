"""Структурированный stdout поток тонкого клиента без серверных зависимостей."""

import json
import logging
import sys
from datetime import UTC, datetime


class JSONFormatter(logging.Formatter):
    """Каждое событие соответствует одной строке JSON."""

    def __init__(self, release_id: str) -> None:
        """Сохранить идентификатор неизменяемого артефакта."""
        super().__init__()
        self.release_id = release_id

    def format(self, record: logging.LogRecord) -> str:
        """Не выводить stack trace с URL файлов и текстом запросов."""
        return json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "release_id": self.release_id,
            },
            ensure_ascii=False,
        )


def configure_logging(*, level: str, release_id: str) -> None:
    """Потоком управляет окружение, локальные файлы логов не создаются."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter(release_id))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
