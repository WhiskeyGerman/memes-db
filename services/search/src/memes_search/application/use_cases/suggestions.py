"""Предложения вариаций без автоматического объединения."""

from uuid import UUID

from memes_search.application.dto.queries import SearchLimit
from memes_search.application.dto.suggestions import SimilarityThreshold, VariationSuggestion
from memes_search.application.ports.catalog import ICatalogReader
from memes_search.application.ports.search_index import ISearchIndex


class SuggestionService:
    """Отбор групп с проверкой порога пары и актуального каталога."""

    def __init__(
        self,
        *,
        index: ISearchIndex,
        catalog: ICatalogReader,
        threshold: float,
        rules: tuple[SimilarityThreshold, ...] = (),
    ) -> None:
        """Сохранить порты и версионируемые пороги отбора."""
        self._index = index
        self._catalog = catalog
        self._threshold = threshold
        self._rules = rules

    async def execute(self, media_id: UUID, limit: int) -> tuple[VariationSuggestion, ...]:
        """Предложить не более десяти близких активных групп."""
        source = await self._catalog.resolve((media_id,))
        if not source:
            return ()
        candidates = await self._index.variation_matches(media_id)
        live = {
            item.media_id: item
            for item in await self._catalog.resolve(
                tuple(dict.fromkeys(item.media_id for item in candidates))
            )
        }
        groups: dict[UUID, VariationSuggestion] = {}
        for candidate in candidates:
            threshold = next(
                (rule.threshold for rule in self._rules if rule.matches(candidate)),
                self._threshold,
            )
            item = live.get(candidate.media_id)
            if item is None or item.group_id == source[0].group_id or candidate.score < threshold:
                continue
            suggestion = VariationSuggestion(
                group_id=item.group_id,
                group_revision=item.group_revision,
                media_id=item.media_id,
                score=candidate.score,
            )
            previous = groups.get(item.group_id)
            if previous is None or (-suggestion.score, suggestion.media_id.int) < (
                -previous.score,
                previous.media_id.int,
            ):
                groups[item.group_id] = suggestion
        maximum = min(limit, SearchLimit.VARIATION_SUGGESTIONS)
        return tuple(
            sorted(groups.values(), key=lambda item: (-item.score, item.group_id.int))[:maximum]
        )
