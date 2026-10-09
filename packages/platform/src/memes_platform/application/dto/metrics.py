"""Нейтральные значения эксплуатации, независимые от Prometheus."""

from dataclasses import dataclass
from enum import StrEnum


class MetricName(StrEnum):
    """Стабильные эксплуатационные показатели."""

    QUEUE_PENDING = "memes_queue_pending"
    QUEUE_OLDEST = "memes_queue_oldest_seconds"
    WORKERS_ONLINE = "memes_workers_online"
    CLEANUP_PENDING = "memes_cleanup_pending"
    CLEANUP_OLDEST = "memes_cleanup_oldest_seconds"
    OUTBOX_PENDING = "memes_outbox_pending"
    OUTBOX_OLDEST = "memes_outbox_oldest_seconds"
    STORAGE_BYTES = "memes_storage_bytes"
    FAILED_TASKS = "memes_failed_tasks"
    TASK_RETRIES = "memes_task_retries"


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricSample:
    """Значение ограниченного предопределённого показателя."""

    name: MetricName
    value: float
