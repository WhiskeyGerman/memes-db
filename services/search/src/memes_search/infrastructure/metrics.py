"""Эксплуатационные агрегаты принадлежащих сервису таблиц."""

from enum import StrEnum

from memes_platform.application.dto.metrics import MetricName


class MetricQuery(StrEnum):
    """Фиксированный SQL без пользовательских значений."""

    OUTBOX_PENDING = "SELECT count(*) FROM search_outbox"

    OUTBOX_OLDEST = (
        "SELECT coalesce(greatest(extract(epoch FROM "
        "now()-min(available_at)),0),0) FROM search_outbox"
    )

    CLEANUP_PENDING = "SELECT count(*) FROM search_samples WHERE expires_at<=now()"


QUERIES = {MetricName[name.name]: name.value for name in MetricQuery}
