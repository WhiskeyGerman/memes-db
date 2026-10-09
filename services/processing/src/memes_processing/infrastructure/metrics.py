"""Эксплуатационные агрегаты принадлежащих сервису таблиц."""

from enum import StrEnum

from memes_platform.application.dto.metrics import MetricName


class MetricQuery(StrEnum):
    """Фиксированный SQL без пользовательских значений."""

    QUEUE_PENDING = (
        "SELECT count(*) FROM processing_tasks WHERE state IN ('waiting','ready','retry_wait')"
    )

    QUEUE_OLDEST = (
        "SELECT coalesce(extract(epoch FROM now()-min(created_at)),0) FROM "
        "processing_tasks WHERE state IN ('waiting','ready','retry_wait')"
    )

    WORKERS_ONLINE = (
        "SELECT count(*) FROM processing_workers WHERE revoked_at IS NULL AND "
        "last_seen_at>now()-interval '90 seconds'"
    )

    CLEANUP_PENDING = (
        "SELECT count(*) FROM processing_artifact_cleanup WHERE NOT retained AND ready_at<=now()"
    )

    CLEANUP_OLDEST = (
        "SELECT coalesce(extract(epoch FROM now()-min(ready_at)),0) FROM "
        "processing_artifact_cleanup WHERE NOT retained AND ready_at<=now()"
    )

    OUTBOX_PENDING = "SELECT count(*) FROM processing_outbox WHERE delivered_at IS NULL"

    OUTBOX_OLDEST = (
        "SELECT coalesce(extract(epoch FROM now()-min(created_at)),0) FROM "
        "processing_outbox WHERE delivered_at IS NULL"
    )

    FAILED_TASKS = "SELECT count(*) FROM processing_tasks WHERE state='failed'"

    TASK_RETRIES = "SELECT count(*) FROM processing_attempts WHERE state IN ('expired','failed')"


QUERIES = {MetricName[name.name]: name.value for name in MetricQuery}
