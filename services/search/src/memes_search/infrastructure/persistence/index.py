"""Полнотекстовый, trigram и векторный поиск PostgreSQL."""

from datetime import UTC, datetime
from enum import StrEnum
from json import JSONDecodeError, loads
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_search.application.dto.projections import (
    EmbeddingVector,
    EmbeddingVersion,
    FeatureKind,
    FeatureProjection,
    FragmentLanguage,
    MediaStateChange,
    ProjectionState,
    TextFragment,
)
from memes_search.application.dto.queries import SearchFilters, SearchLimit, SearchQuery
from memes_search.application.dto.samples import SampleFeatures
from memes_search.application.dto.suggestions import VariationMatch
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.application.ports.search_index import ISearchIndex
from memes_search.domain.fragments import MatchedSpan
from memes_search.domain.ranking import MediaCandidate, RankingLimit
from memes_search.infrastructure.configuration import SearchDefault
from memes_search.infrastructure.persistence.models import (
    FeatureRow,
    InboxRow,
    MediaRow,
    TextFragmentRow,
    VectorRow,
)

EMBEDDING_VERSION = TypeAdapter(EmbeddingVersion)


class SearchSql(StrEnum):
    """Параметризованные SQL-ветви с ранжированием по лучшему фрагменту."""

    FILTER = """
        m.active AND (:all_kinds OR m.media_kind = ANY(CAST(:kinds AS text[])))
        AND (CAST(:after AS timestamptz) IS NULL OR m.published_at >= CAST(:after AS timestamptz))
        AND (CAST(:before AS timestamptz) IS NULL OR m.published_at < CAST(:before AS timestamptz))
    """
    FULL_TEXT = """
        WITH scored AS (
          SELECT m.media_id, m.group_id, f.kind, f.ordinal, f.start_ms, f.end_ms,
            ts_rank_cd(f.search_vector, websearch_to_tsquery('russian', :query) ||
              websearch_to_tsquery('english', :query)) AS score
          FROM search_text_fragments f JOIN search_media m USING (media_id)
          WHERE f.search_vector @@ (websearch_to_tsquery('russian', :query) ||
            websearch_to_tsquery('english', :query)) AND {filters}
        ), best AS (
          SELECT DISTINCT ON (media_id) * FROM scored
          ORDER BY media_id, score DESC, kind, ordinal
        )
        SELECT media_id, group_id, score, start_ms, end_ms FROM best
        ORDER BY score DESC, media_id LIMIT :limit
    """
    FUZZY = """
        WITH scored AS (
          SELECT m.media_id, m.group_id, f.kind, f.ordinal, f.start_ms, f.end_ms,
            word_similarity(:query, f.text) AS score
          FROM search_text_fragments f JOIN search_media m USING (media_id)
          WHERE :query <% f.text AND {filters}
        ), best AS (
          SELECT DISTINCT ON (media_id) * FROM scored
          ORDER BY media_id, score DESC, kind, ordinal
        )
        SELECT media_id, group_id, score, start_ms, end_ms FROM best
        ORDER BY score DESC, media_id LIMIT :limit
    """
    VECTOR = """
        WITH scored AS (
          SELECT m.media_id, m.group_id, v.kind, v.ordinal, v.start_ms, v.end_ms,
            1 - (v."values" <=> CAST(:vector AS vector)) AS score
          FROM search_vectors v JOIN search_media m USING (media_id)
          JOIN search_features f ON f.media_id = v.media_id AND f.kind = v.kind
          WHERE v.space_id = :space AND vector_dims(v."values") = :dimensions
            AND (CASE WHEN f.version_key IS JSON OBJECT THEN f.version_key::jsonb
                      ELSE '{{}}'::jsonb END)->>'model' = :model
            AND (CASE WHEN f.version_key IS JSON OBJECT THEN f.version_key::jsonb
                      ELSE '{{}}'::jsonb END)->>'weights' = :weights
            AND (CASE WHEN f.version_key IS JSON OBJECT THEN f.version_key::jsonb
                      ELSE '{{}}'::jsonb END)->>'preprocessing' = :preprocessing
            AND (CASE WHEN f.version_key IS JSON OBJECT THEN f.version_key::jsonb
                      ELSE '{{}}'::jsonb END)->>'task_adapter' = :task_adapter AND {filters}
        ), best AS (
          SELECT DISTINCT ON (media_id) * FROM scored
          ORDER BY media_id, score DESC, kind, ordinal
        )
        SELECT media_id, group_id, score, start_ms, end_ms FROM best
        ORDER BY score DESC, media_id LIMIT :limit
    """
    ANN_VECTOR = """
        WITH nearest AS MATERIALIZED (
          SELECT v.media_id, m.group_id, v.kind, v.ordinal, v.start_ms, v.end_ms, v."values"
          FROM search_vectors v JOIN search_media m USING (media_id)
          JOIN search_features f ON f.media_id = v.media_id AND f.kind = v.kind
          WHERE v.space_id = :space AND vector_dims(v."values") = 768
            AND (CASE WHEN f.version_key IS JSON OBJECT THEN f.version_key::jsonb
                      ELSE '{{}}'::jsonb END)->>'model' = :model
            AND (CASE WHEN f.version_key IS JSON OBJECT THEN f.version_key::jsonb
                      ELSE '{{}}'::jsonb END)->>'weights' = :weights
            AND (CASE WHEN f.version_key IS JSON OBJECT THEN f.version_key::jsonb
                      ELSE '{{}}'::jsonb END)->>'preprocessing' = :preprocessing
            AND (CASE WHEN f.version_key IS JSON OBJECT THEN f.version_key::jsonb
                      ELSE '{{}}'::jsonb END)->>'task_adapter' = :task_adapter AND {filters}
          ORDER BY v."values"::halfvec(768) <=> CAST(:vector AS halfvec(768))
          LIMIT :fragment_limit
        ), scored AS (
          SELECT *, 1 - ("values" <=> CAST(:vector AS vector)) AS score FROM nearest
        ), best AS (
          SELECT DISTINCT ON (media_id) * FROM scored
          ORDER BY media_id, score DESC, kind, ordinal
        )
        SELECT media_id, group_id, score, start_ms, end_ms FROM best
        ORDER BY score DESC, media_id LIMIT :limit
    """
    VARIATIONS = """
        WITH nearest AS MATERIALIZED (
          SELECT v.media_id, v.kind, v."values"
          FROM search_vectors v
          JOIN search_media m USING (media_id)
          JOIN search_features f ON f.media_id = v.media_id AND f.kind = v.kind
          WHERE m.active AND v.media_id <> :source_id
            AND v.space_id = :space AND vector_dims(v."values") = :dimensions
            AND (CASE WHEN f.version_key IS JSON OBJECT THEN f.version_key::jsonb
                      ELSE '{{}}'::jsonb END)->>'model' = :model
            AND (CASE WHEN f.version_key IS JSON OBJECT THEN f.version_key::jsonb
                      ELSE '{{}}'::jsonb END)->>'weights' = :weights
            AND (CASE WHEN f.version_key IS JSON OBJECT THEN f.version_key::jsonb
                      ELSE '{{}}'::jsonb END)->>'preprocessing' = :preprocessing
            AND (CASE WHEN f.version_key IS JSON OBJECT THEN f.version_key::jsonb
                      ELSE '{{}}'::jsonb END)->>'task_adapter' = :task_adapter
          ORDER BY {distance}
          LIMIT :fragment_limit
        )
        SELECT media_id, kind, MAX(1 - ("values" <=> CAST(:vector AS vector))) AS score
        FROM nearest GROUP BY media_id, kind ORDER BY score DESC, media_id LIMIT :limit
    """
    HALF_DISTANCE = 'v."values"::halfvec(768) <=> CAST(:vector AS halfvec(768))'
    FULL_DISTANCE = 'v."values" <=> CAST(:vector AS vector)'
    ANN_SCAN = "SELECT set_config('hnsw.iterative_scan', 'strict_order', true)"
    TRIGRAM_THRESHOLD = "SELECT set_config('pg_trgm.word_similarity_threshold', :threshold, true)"


class PostgreSQLSearchIndex(ISearchIndex):
    """Восстанавливаемые признаки, inbox и tombstone в одной транзакции."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        fuzzy_threshold: float,
        ann_fragment_multiplier: int = SearchDefault.ANN_FRAGMENT_MULTIPLIER,
    ) -> None:
        """Сохранить ограниченный пул и порог нечёткого поиска."""
        self._factory = factory
        self._fuzzy_threshold = fuzzy_threshold
        self._ann_fragment_multiplier = ann_fragment_multiplier

    async def text(self, query: SearchQuery) -> tuple[tuple[MediaCandidate, ...], ...]:
        """Получить независимые полнотекстовую и нечёткую ветви."""
        parameters = self._filters(query.filters)
        parameters["query"] = query.text or ""
        try:
            async with self._factory() as session, session.begin():
                await session.execute(
                    text(SearchSql.TRIGRAM_THRESHOLD), {"threshold": str(self._fuzzy_threshold)}
                )
                full = await self._fetch(session, SearchSql.FULL_TEXT, parameters)
                fuzzy = await self._fetch(session, SearchSql.FUZZY, parameters)
                return full, fuzzy
        except (DBAPIError, SQLAlchemyError) as error:
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error

    async def vectors(
        self,
        query: SearchQuery,
        vectors: tuple[EmbeddingVector, ...],
    ) -> tuple[MediaCandidate, ...]:
        """Схлопнуть фрагменты по лучшему косинусному совпадению медиа."""
        best: dict[UUID, MediaCandidate] = {}
        try:
            async with self._factory() as session, session.begin():
                await session.execute(text(SearchSql.ANN_SCAN))
                for vector in vectors:
                    if vector.version is None:
                        raise SearchError(SearchErrorCode.UNAVAILABLE)
                    parameters = self._filters(query.filters)
                    parameters.update(
                        {
                            "vector": self._vector_text(vector.values),
                            "space": vector.space_id,
                            "dimensions": len(vector.values),
                            "model": vector.version.model,
                            "weights": vector.version.weights,
                            "preprocessing": vector.version.preprocessing,
                            "task_adapter": vector.version.task_adapter,
                        }
                    )
                    parameters["fragment_limit"] = (
                        RankingLimit.CANDIDATES_PER_BRANCH * self._ann_fragment_multiplier
                    )
                    statement = (
                        SearchSql.ANN_VECTOR
                        if len(vector.values) == SearchLimit.EMBEDDING_INDEX_DIMENSIONS
                        else SearchSql.VECTOR
                    )
                    for candidate in await self._fetch(session, statement, parameters):
                        previous = best.get(candidate.media_id)
                        if previous is None or candidate.score > previous.score:
                            best[candidate.media_id] = candidate
        except (DBAPIError, SQLAlchemyError) as error:
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error
        return tuple(
            sorted(best.values(), key=lambda item: (-item.score, item.media_id.int))[
                : RankingLimit.CANDIDATES_PER_BRANCH
            ]
        )

    async def variation_matches(self, media_id: UUID) -> tuple[VariationMatch, ...]:
        """Сравнить модальности при совпадении модели, весов и preprocessing."""
        best: dict[tuple[UUID, str, str, FeatureKind, FeatureKind], VariationMatch] = {}
        try:
            async with self._factory() as session, session.begin():
                await session.execute(text(SearchSql.ANN_SCAN))
                sources = (
                    await session.execute(
                        select(VectorRow, FeatureRow)
                        .join(
                            FeatureRow,
                            (VectorRow.media_id == FeatureRow.media_id)
                            & (VectorRow.kind == FeatureRow.kind),
                        )
                        .join(MediaRow, MediaRow.media_id == VectorRow.media_id)
                        .where(VectorRow.media_id == media_id, MediaRow.active.is_(True))
                    )
                ).all()
                for vector, feature in sources:
                    try:
                        raw_version: object = loads(feature.version_key)
                    except JSONDecodeError:
                        continue
                    if not isinstance(raw_version, dict):
                        continue
                    model, weights, preprocessing, task_adapter = (
                        raw_version.get("model"),
                        raw_version.get("weights"),
                        raw_version.get("preprocessing"),
                        raw_version.get("task_adapter"),
                    )
                    if not all(
                        isinstance(item, str) and item
                        for item in (model, weights, preprocessing, task_adapter)
                    ):
                        continue
                    dimensions = len(vector.values)
                    parameters = {
                        "source_id": media_id,
                        "space": vector.space_id,
                        "dimensions": dimensions,
                        "vector": self._vector_text(tuple(vector.values)),
                        "model": model,
                        "weights": weights,
                        "preprocessing": preprocessing,
                        "task_adapter": task_adapter,
                        "fragment_limit": RankingLimit.CANDIDATES_PER_BRANCH
                        * self._ann_fragment_multiplier,
                        "limit": RankingLimit.CANDIDATES_PER_BRANCH,
                    }
                    distance = (
                        SearchSql.HALF_DISTANCE
                        if dimensions == SearchLimit.EMBEDDING_INDEX_DIMENSIONS
                        else SearchSql.FULL_DISTANCE
                    )
                    statement = text(SearchSql.VARIATIONS.format(distance=distance))
                    for row in (await session.execute(statement, parameters)).mappings():
                        candidate = VariationMatch(
                            media_id=row["media_id"],
                            score=float(row["score"]),
                            space_id=vector.space_id,
                            model_revision=f"{model}@{weights}",
                            source_kind=FeatureKind(vector.kind),
                            target_kind=FeatureKind(row["kind"]),
                        )
                        key = (
                            candidate.media_id,
                            candidate.space_id,
                            candidate.model_revision,
                            candidate.source_kind,
                            candidate.target_kind,
                        )
                        previous = best.get(key)
                        if previous is None or candidate.score > previous.score:
                            best[key] = candidate
        except (DBAPIError, SQLAlchemyError) as error:
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error
        return tuple(sorted(best.values(), key=lambda item: (-item.score, item.media_id.int)))

    async def features_for(self, media_id: UUID) -> SampleFeatures:
        """Получить признаки для повторного использования только активного оригинала."""
        async with self._factory() as session:
            media = await session.get(MediaRow, media_id)
            if media is None or not media.active:
                return SampleFeatures()
            texts = (
                await session.scalars(
                    select(FeatureRow.text).where(FeatureRow.media_id == media_id)
                )
            ).all()
            rows = (
                await session.execute(
                    select(VectorRow, FeatureRow.version_key)
                    .join(
                        FeatureRow,
                        (FeatureRow.media_id == VectorRow.media_id)
                        & (FeatureRow.kind == VectorRow.kind),
                    )
                    .where(VectorRow.media_id == media_id)
                )
            ).all()
            return SampleFeatures(
                text=" ".join(value for value in texts if value),
                vectors=tuple(
                    EmbeddingVector(
                        values=tuple(float(value) for value in row.values),
                        space_id=row.space_id,
                        version=EMBEDDING_VERSION.validate_json(version_key),
                        span=MatchedSpan(start_ms=row.start_ms, end_ms=row.end_ms)
                        if row.start_ms is not None and row.end_ms is not None
                        else None,
                    )
                    for row, version_key in rows
                ),
            )

    async def apply_feature(self, projection: FeatureProjection) -> bool:
        """Идемпотентно применить последнее поколение без воскрешения файла."""
        async with self._factory() as session, session.begin():
            if not await self._claim(session, projection.event_id):
                return False
            await session.execute(
                insert(MediaRow)
                .values(
                    media_id=projection.media_id,
                    group_id=projection.group_id,
                    group_revision=projection.group_revision,
                    revision=projection.revision,
                    active=True,
                    media_kind=projection.media_kind,
                    published_at=projection.published_at,
                )
                .on_conflict_do_nothing(index_elements=[MediaRow.media_id])
            )
            media = await session.scalar(
                select(MediaRow).where(MediaRow.media_id == projection.media_id).with_for_update()
            )
            if media is None or not media.active:
                return False
            feature = await session.get(FeatureRow, (projection.media_id, projection.kind))
            if feature is not None and (
                feature.generation > projection.generation
                or (
                    feature.generation == projection.generation
                    and (
                        feature.cycle_id != projection.cycle_id
                        or feature.state != ProjectionState.PENDING
                        or projection.state is ProjectionState.PENDING
                    )
                )
            ):
                return False
            if projection.revision >= media.revision:
                media.group_id = projection.group_id
                media.group_revision = projection.group_revision
                media.revision = projection.revision
                media.media_kind = projection.media_kind
                media.published_at = projection.published_at
            if feature is None:
                feature = FeatureRow(
                    media_id=projection.media_id,
                    kind=projection.kind,
                    generation=projection.generation,
                    cycle_id=projection.cycle_id,
                    state=projection.state,
                    version_key=projection.version_key,
                    text="",
                )
                session.add(feature)
            else:
                feature.generation = projection.generation
                feature.cycle_id = projection.cycle_id
                feature.state = projection.state
            if projection.state in {ProjectionState.AVAILABLE, ProjectionState.EMPTY}:
                feature.version_key = projection.version_key
                feature.text = projection.text
                await session.execute(
                    delete(VectorRow).where(
                        VectorRow.media_id == projection.media_id, VectorRow.kind == projection.kind
                    )
                )
                await session.execute(
                    delete(TextFragmentRow).where(
                        TextFragmentRow.media_id == projection.media_id,
                        TextFragmentRow.kind == projection.kind,
                    )
                )
                await session.flush()
                fragments = projection.fragments or (
                    (TextFragment(text=projection.text, language=FragmentLanguage.UNSPECIFIED),)
                    if projection.text
                    else ()
                )
                session.add_all(
                    TextFragmentRow(
                        media_id=projection.media_id,
                        kind=projection.kind,
                        ordinal=ordinal,
                        text=fragment.text,
                        language=fragment.language,
                        start_ms=fragment.span.start_ms if fragment.span else None,
                        end_ms=fragment.span.end_ms if fragment.span else None,
                    )
                    for ordinal, fragment in enumerate(fragments)
                )
                session.add_all(
                    VectorRow(
                        media_id=projection.media_id,
                        kind=projection.kind,
                        ordinal=ordinal,
                        space_id=vector.space_id,
                        values=list(vector.values),
                        start_ms=vector.span.start_ms if vector.span else None,
                        end_ms=vector.span.end_ms if vector.span else None,
                    )
                    for ordinal, vector in enumerate(projection.vectors)
                )
            return True

    async def apply_media_state(self, change: MediaStateChange) -> bool:
        """Применить членство или вечный tombstone с монотонной ревизией."""
        async with self._factory() as session, session.begin():
            if not await self._claim(session, change.event_id):
                return False
            inserted = await session.scalar(
                insert(MediaRow)
                .values(
                    media_id=change.media_id,
                    group_id=change.group_id,
                    group_revision=change.group_revision,
                    revision=change.revision,
                    active=change.active,
                    media_kind=change.kind,
                    published_at=change.published_at,
                )
                .on_conflict_do_nothing(index_elements=[MediaRow.media_id])
                .returning(MediaRow.media_id)
            )
            if inserted is not None:
                return True
            media = await session.scalar(
                select(MediaRow).where(MediaRow.media_id == change.media_id).with_for_update()
            )
            if (
                media is None
                or change.revision <= media.revision
                or (not media.active and change.active)
            ):
                return False
            media.revision = change.revision
            media.active = change.active
            media.group_id = change.group_id
            media.group_revision = change.group_revision
            if change.kind is not None:
                media.media_kind = change.kind
            if change.published_at is not None:
                media.published_at = change.published_at
            if not change.active:
                await session.execute(
                    delete(FeatureRow).where(FeatureRow.media_id == change.media_id)
                )
            return True

    @staticmethod
    async def _claim(session: AsyncSession, event_id: UUID) -> bool:
        return (
            await session.scalar(
                insert(InboxRow)
                .values(event_id=event_id, received_at=datetime.now(UTC))
                .on_conflict_do_nothing(index_elements=[InboxRow.event_id])
                .returning(InboxRow.event_id)
            )
            is not None
        )

    @staticmethod
    def _filters(filters: SearchFilters) -> dict[str, object]:
        return {
            "all_kinds": not filters.kinds,
            "kinds": [kind.value for kind in filters.kinds],
            "after": filters.published_after,
            "before": filters.published_before,
            "limit": RankingLimit.CANDIDATES_PER_BRANCH,
        }

    @staticmethod
    def _vector_text(values: tuple[float, ...]) -> str:
        return "[" + ",".join(str(value) for value in values) + "]"

    @staticmethod
    async def _fetch(
        session: AsyncSession,
        statement: SearchSql,
        parameters: dict[str, object],
    ) -> tuple[MediaCandidate, ...]:
        rows = (
            (await session.execute(text(statement.format(filters=SearchSql.FILTER)), parameters))
            .mappings()
            .all()
        )
        return tuple(
            MediaCandidate(
                media_id=UUID(str(row["media_id"])),
                group_id=UUID(str(row["group_id"])),
                score=float(row["score"]),
                matched_span=MatchedSpan(start_ms=int(row["start_ms"]), end_ms=int(row["end_ms"]))
                if row["start_ms"] is not None and row["end_ms"] is not None
                else None,
            )
            for row in rows
        )
