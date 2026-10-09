"""Сценарии поиска с авторитетной проверкой текущего каталога."""

from dataclasses import replace
from uuid import UUID, uuid4

from memes_kernel.values import Number, Revision
from memes_search.application.dto.execution import SearchExecution, SearchPending, SearchRequest
from memes_search.application.dto.projections import CatalogMedia, EmbeddingVector
from memes_search.application.dto.queries import SearchLimit, SearchMode, SearchQuery
from memes_search.application.dto.samples import InferenceState, SampleState
from memes_search.application.dto.sessions import (
    GroupSnapshot,
    SearchPage,
    SearchSession,
    SearchSessionPolicy,
)
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.application.ports.catalog import ICatalogReader
from memes_search.application.ports.clock import IClock
from memes_search.application.ports.inference import IInferenceGateway
from memes_search.application.ports.requests import ISearchRequestRepository
from memes_search.application.ports.samples import ISampleRepository
from memes_search.application.ports.search_index import ISearchIndex
from memes_search.application.ports.sessions import ISearchSessionRepository
from memes_search.domain.ranking import MediaCandidate, RankingPolicy


class SearchService:
    """RRF, стабильный снимок и live-проверка каждого ответа каталога."""

    def __init__(
        self,
        *,
        index: ISearchIndex,
        sessions: ISearchSessionRepository,
        requests: ISearchRequestRepository,
        catalog: ICatalogReader,
        inference: IInferenceGateway,
        samples: ISampleRepository,
        clock: IClock,
        space_id: str,
        ranking: RankingPolicy,
        session_policy: SearchSessionPolicy,
    ) -> None:
        """Получить независимые порты и продуктовые политики."""
        self._index = index
        self._sessions = sessions
        self._requests = requests
        self._catalog = catalog
        self._inference = inference
        self._samples = samples
        self._clock = clock
        self._space_id = space_id
        self._ranking = ranking
        self._session_policy = session_policy

    async def execute(self, query: SearchQuery, request_id: UUID) -> SearchExecution:
        """Начать запрос или идемпотентно продолжить ожидающий inference."""
        now = self._clock.now()
        if (
            query.text is not None
            and len(query.text) > self._session_policy.maximum_query_characters
        ):
            raise SearchError(SearchErrorCode.INVALID_QUERY)
        request = await self._requests.get(request_id)
        if request is not None:
            if request.query != query:
                raise SearchError(SearchErrorCode.INVALID_QUERY)
            if request.expires_at <= now:
                raise SearchError(SearchErrorCode.SESSION_EXPIRED)
            if request.session_id is not None:
                return await self.page(
                    request.session_id, query.owner_id, Number.ZERO, query.page_size
                )
        else:
            request = SearchRequest(
                id=request_id,
                query=query,
                created_at=now,
                expires_at=now + self._session_policy.lifetime,
            )
            await self._requests.save(request)
        branches: tuple[tuple[MediaCandidate, ...], ...] = ()
        degraded = False
        exact_id: UUID | None = None
        vectors: tuple[EmbeddingVector, ...] = ()
        if query.mode in {SearchMode.TEXT, SearchMode.HYBRID}:
            branches = await self._index.text(query)
        if query.mode is SearchMode.SAMPLE:
            if query.sample_id is None:
                raise SearchError(SearchErrorCode.INVALID_QUERY)
            sample = await self._samples.get(query.sample_id)
            if sample is None or sample.expires_at <= now:
                raise SearchError(SearchErrorCode.NOT_FOUND)
            if sample.owner_id != query.owner_id:
                raise SearchError(SearchErrorCode.FORBIDDEN)
            if sample.state in {SampleState.CANCELLED, SampleState.FAILED}:
                raise SearchError(SearchErrorCode.SAMPLE_FAILED)
            if sample.state is not SampleState.READY:
                return SearchPending(
                    request_id=request.id, expires_at=request.expires_at, task_id=None
                )
            features = await self._samples.features(sample.id)
            vectors = features.vectors
            exact_id = sample.exact_media_id
            if features.text:
                branches = await self._index.text(
                    replace(
                        query,
                        mode=SearchMode.TEXT,
                        text=features.text[: SearchLimit.QUERY_CHARACTERS],
                        sample=None,
                        sample_id=None,
                    )
                )
        elif query.mode in {SearchMode.HYBRID, SearchMode.SEMANTIC}:
            try:
                result = (
                    await self._inference.query_result(request.inference_task_id)
                    if request.inference_task_id is not None
                    else await self._inference.query(
                        request.id, query.text or "", self._space_id, query.owner_id
                    )
                )
                request = replace(request, inference_task_id=result.task_id)
                await self._requests.save(request)
                if result.state is InferenceState.SUCCEEDED and result.embedding is not None:
                    if result.embedding.space_id != self._space_id:
                        raise SearchError(SearchErrorCode.UNAVAILABLE)
                    vectors = (result.embedding,)
                elif query.mode is SearchMode.SEMANTIC:
                    if result.state in {InferenceState.FAILED, InferenceState.CANCELLED}:
                        raise SearchError(SearchErrorCode.INFERENCE_FAILED)
                    return SearchPending(
                        request_id=request.id, expires_at=request.expires_at, task_id=result.task_id
                    )
                else:
                    degraded = True
            except SearchError as error:
                if query.mode is SearchMode.SEMANTIC or error.code not in {
                    SearchErrorCode.UNAVAILABLE,
                    SearchErrorCode.INFERENCE_FAILED,
                }:
                    raise
                degraded = True
        if vectors:
            try:
                branches += (await self._index.vectors(query, vectors),)
            except SearchError as error:
                if (
                    query.mode is not SearchMode.HYBRID
                    or error.code is not SearchErrorCode.UNAVAILABLE
                ):
                    raise
                degraded = True
        ids = tuple({candidate.media_id for branch in branches for candidate in branch})
        if exact_id is not None and exact_id not in ids:
            ids += (exact_id,)
        live = {item.media_id: item for item in await self._catalog.resolve(ids)}
        branches = tuple(
            tuple(
                replace(item, group_id=live[item.media_id].group_id)
                for item in branch
                if item.media_id in live and self._matches(live[item.media_id], query)
            )
            for branch in branches
        )
        if exact_id is not None and exact_id in live and self._matches(live[exact_id], query):
            branches += (
                (
                    MediaCandidate(
                        media_id=exact_id, group_id=live[exact_id].group_id, score=float(Number.ONE)
                    ),
                ),
            )
        if self._clock.now() >= request.expires_at:
            raise SearchError(SearchErrorCode.SESSION_EXPIRED)
        ranked = self._ranking.fuse(branches, exact_media_id=exact_id)
        session = SearchSession(
            id=uuid4(),
            query=query,
            created_at=now,
            expires_at=request.expires_at,
            results=ranked,
            degraded=degraded,
            groups=tuple(
                GroupSnapshot(
                    group_id=item.group_id,
                    composition_revision=Revision(
                        value=live[item.representative_id].group_revision
                    ),
                )
                for item in ranked
            ),
        )
        await self._sessions.save(session)
        await self._requests.save(replace(request, session_id=session.id))
        return await self.page(session.id, query.owner_id, Number.ZERO, query.page_size)

    async def resume(self, request_id: UUID, owner_id: UUID | None) -> SearchExecution:
        """Продолжить только доступный владельцу приватный запрос."""
        request = await self._requests.get(request_id)
        if request is None:
            raise SearchError(SearchErrorCode.NOT_FOUND)
        if request.query.owner_id != owner_id:
            raise SearchError(SearchErrorCode.FORBIDDEN)
        return await self.execute(request.query, request.id)

    async def page(
        self,
        session_id: UUID,
        owner_id: UUID | None,
        offset: int,
        limit: int,
    ) -> SearchPage:
        """Отфильтровать удалённые файлы и отклонить сменившееся членство."""
        if offset < Number.ZERO or not Number.ONE <= limit <= SearchLimit.MAXIMUM_PAGE_SIZE:
            raise SearchError(SearchErrorCode.INVALID_QUERY)
        session = await self._sessions.get(session_id)
        if session is None:
            raise SearchError(SearchErrorCode.NOT_FOUND)
        if session.query.owner_id != owner_id:
            raise SearchError(SearchErrorCode.FORBIDDEN)
        now = self._clock.now()
        if now >= session.expires_at:
            raise SearchError(SearchErrorCode.SESSION_EXPIRED)
        live = {
            item.media_id: item
            for item in await self._catalog.resolve(
                tuple(item.representative_id for item in session.results)
            )
        }
        expected = {item.group_id: item.composition_revision.value for item in session.groups}
        for result in session.results:
            current = live.get(result.representative_id)
            if current is not None and (
                current.group_id != result.group_id
                or current.group_revision != expected[result.group_id]
            ):
                raise SearchError(SearchErrorCode.GROUPS_CHANGED)
        valid = tuple(item for item in session.results if item.representative_id in live)
        selected = session.results[offset : offset + limit]
        page = tuple(item for item in selected if item.representative_id in live)
        next_offset = offset + limit if offset + limit < len(session.results) else None
        return SearchPage(
            session_id=session.id,
            expires_at=session.expires_at,
            results=page,
            next_offset=next_offset,
            degraded=session.degraded,
            window_size=len(valid),
            offset=offset,
            page_size=limit,
        )

    @staticmethod
    def _matches(item: CatalogMedia, query: SearchQuery) -> bool:
        return (
            (not query.filters.kinds or item.kind in query.filters.kinds)
            and (
                query.filters.published_after is None
                or item.published_at >= query.filters.published_after
            )
            and (
                query.filters.published_before is None
                or item.published_at < query.filters.published_before
            )
        )
