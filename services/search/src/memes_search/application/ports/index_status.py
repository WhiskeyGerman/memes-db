"""Порт read-only измерения покрытия индекса."""

from abc import ABC, abstractmethod

from memes_search.application.dto.index_status import IndexCoverage


class IIndexStatusReader(ABC):
    """Измерить текущие проекции без раскрытия медиа и поисковых запросов."""

    @abstractmethod
    async def coverage(self) -> IndexCoverage:
        """Прочитать согласованную сводку из внешнего хранилища."""
