"""Эксплуатационные агрегаты принадлежащих сервису таблиц."""

from enum import StrEnum

from memes_platform.application.dto.metrics import MetricName


class MetricQuery(StrEnum):
    """Фиксированный SQL без пользовательских значений."""

    MEDIA_PUBLISHED = "SELECT count(*) FROM catalog_media WHERE state='published'"

    GROUPS_ACTIVE = "SELECT count(*) FROM catalog_groups WHERE state='active'"

    BALLOTS_OPEN = "SELECT count(*) FROM catalog_ballots WHERE state='open'"

    OUTBOX_PENDING = "SELECT count(*) FROM catalog_outbox WHERE delivered_at IS NULL"

    OUTBOX_OLDEST = (
        "SELECT coalesce(extract(epoch FROM now()-min(created_at)),0) FROM "
        "catalog_outbox WHERE delivered_at IS NULL"
    )

    CLEANUP_PENDING = (
        "SELECT count(*) FROM catalog_cleanup WHERE completed_at IS NULL AND created_at<=now()"
    )

    CLEANUP_OLDEST = (
        "SELECT coalesce(extract(epoch FROM now()-min(created_at)),0) FROM "
        "catalog_cleanup WHERE completed_at IS NULL AND created_at<=now()"
    )

    STORAGE_BYTES = "SELECT coalesce(sum(size_bytes),0) FROM catalog_media WHERE state='published'"


QUERIES = {MetricName[name.name]: name.value for name in MetricQuery}
