"""Адресуемый deployment поискового индекса в доверенном REST."""

from enum import StrEnum


class IndexTarget(StrEnum):
    """Стабильные backing aliases при переключении публичного маршрута."""

    PRIMARY = "primary"
    SHADOW = "shadow"
