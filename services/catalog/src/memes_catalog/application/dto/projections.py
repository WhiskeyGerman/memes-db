"""Чистая сериализация согласованных снимков каталога."""

from memes_catalog.application.dto.catalog import MediaView
from memes_platform.application.dto.json import JsonObject


def media_payload(view: MediaView) -> JsonObject:
    """Сериализовать живой согласованный снимок.

    Args:
        view: Снимок публикации.
    """
    return {
        "media_id": str(view.media.id),
        "group_id": str(view.group.id),
        "group_revision": view.group.revision.value,
        "composition_revision": view.group.composition_revision.value,
        "revision": view.projection_revision,
        "source_revision": view.media.revision.value,
        "published_at": view.media.published_at.isoformat(),
        "media_kind": view.media.original.kind.value,
        "kind": view.media.original.kind.value,
        "author_id": str(view.media.author_id),
        "sha256": view.media.original.file.sha256.value,
        "file_id": str(view.media.original.file.id),
        "active": view.media.deleted_at is None,
    }
