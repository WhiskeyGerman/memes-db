"""Продуктовый порядок выбора обложки группы вариаций."""

from enum import IntEnum

from memes_catalog.domain.media import MediaKind


class CoverPriority(IntEnum):
    """Порядок пригодности медиа для обложки."""

    IMAGE = 0
    ANIMATION = 1
    VIDEO = 2
    AUDIO = 3


COVER_PRIORITIES = {
    MediaKind.IMAGE: CoverPriority.IMAGE,
    MediaKind.ANIMATION: CoverPriority.ANIMATION,
    MediaKind.VIDEO: CoverPriority.VIDEO,
    MediaKind.AUDIO: CoverPriority.AUDIO,
}
