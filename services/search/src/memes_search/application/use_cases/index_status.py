"""Измерить модельное поколение и покрытие перед переключением поиска."""

from memes_search.application.dto.index_status import IndexStatus
from memes_search.application.dto.projections import EmbeddingVersion
from memes_search.application.ports.clock import IClock
from memes_search.application.ports.index_status import IIndexStatusReader


class InspectIndex:
    """Read-only административная сводка текущего deployment."""

    def __init__(
        self,
        *,
        reader: IIndexStatusReader,
        clock: IClock,
        accepted_version: EmbeddingVersion,
        release_id: str,
    ) -> None:
        """Получить независимый reader и выбранное модельное поколение."""
        self._reader = reader
        self._clock = clock
        self._accepted_version = accepted_version
        self._release_id = release_id

    async def execute(self) -> IndexStatus:
        """Получить counts, которые оператор сравнивает с Catalog и lag."""
        return IndexStatus(
            release_id=self._release_id,
            accepted_embedding_version=self._accepted_version,
            measured_at=self._clock.now(),
            coverage=await self._reader.coverage(),
        )
