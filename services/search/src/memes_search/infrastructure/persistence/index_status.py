"""Read-only SQL-сводка покрытия в одной snapshot-транзакции."""

from enum import StrEnum

from sqlalchemy import distinct, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_kernel.values import Number
from memes_search.application.dto.index_status import FeatureCoverage, IndexCoverage
from memes_search.application.dto.projections import FeatureKind, ProjectionState
from memes_search.application.ports.index_status import IIndexStatusReader
from memes_search.infrastructure.persistence.models import (
    FeatureRow,
    MediaRow,
    TextFragmentRow,
    VectorRow,
)


class IndexStatusSql(StrEnum):
    """Один согласованный read-only снимок."""

    SNAPSHOT = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"


class PostgreSQLIndexStatus(IIndexStatusReader):
    """REPEATABLE READ измерение без изменения индекса."""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """Сохранить фабрику снимка статистики."""
        self._factory = factory

    async def coverage(self) -> IndexCoverage:
        """Прочитать счётчики из одного состояния PostgreSQL."""
        async with self._factory() as session, session.begin():
            await session.execute(text(IndexStatusSql.SNAPSHOT))
            active = await session.scalar(
                select(func.count()).select_from(MediaRow).where(MediaRow.active.is_(True))
            )
            tombstones = await session.scalar(
                select(func.count()).select_from(MediaRow).where(MediaRow.active.is_(False))
            )
            vectors = await session.scalar(
                select(func.count(distinct(VectorRow.media_id)))
                .join(MediaRow, MediaRow.media_id == VectorRow.media_id)
                .where(MediaRow.active.is_(True))
            )
            texts = await session.scalar(
                select(func.count(distinct(TextFragmentRow.media_id)))
                .join(MediaRow, MediaRow.media_id == TextFragmentRow.media_id)
                .where(MediaRow.active.is_(True), TextFragmentRow.text != "")
            )
            rows = (
                await session.execute(
                    select(FeatureRow.kind, FeatureRow.state, FeatureRow.version_key, func.count())
                    .join(MediaRow, MediaRow.media_id == FeatureRow.media_id)
                    .where(MediaRow.active.is_(True))
                    .group_by(FeatureRow.kind, FeatureRow.state, FeatureRow.version_key)
                    .order_by(FeatureRow.kind, FeatureRow.state, FeatureRow.version_key)
                )
            ).all()
            return IndexCoverage(
                active_media=active or Number.ZERO,
                tombstones=tombstones or Number.ZERO,
                vector_media=vectors or Number.ZERO,
                text_media=texts or Number.ZERO,
                features=tuple(
                    FeatureCoverage(
                        kind=FeatureKind(kind),
                        state=ProjectionState(state),
                        version_key=version,
                        media_count=count,
                    )
                    for kind, state, version, count in rows
                ),
            )
