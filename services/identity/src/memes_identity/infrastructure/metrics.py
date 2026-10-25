"""Эксплуатационные агрегаты принадлежащих сервису таблиц."""

from enum import StrEnum

from memes_platform.application.dto.metrics import MetricName


class MetricQuery(StrEnum):
    """Фиксированный SQL без пользовательских значений."""

    USERS_ACTIVE = "SELECT count(*) FROM identity_users WHERE state='active'"

    OUTBOX_PENDING = "SELECT count(*) FROM identity_outbox WHERE delivered_at IS NULL"

    OUTBOX_OLDEST = (
        "SELECT coalesce(extract(epoch FROM now()-min(occurred_at)),0) FROM "
        "identity_outbox WHERE delivered_at IS NULL"
    )


QUERIES = {MetricName[name.name]: name.value for name in MetricQuery}
