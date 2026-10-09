"""Приём только совместимого модельного поколения текущего индекса."""

from memes_search.application.dto.projections import (
    EmbeddingVersion,
    FeatureKind,
    FeatureProjection,
)
from memes_search.application.dto.samples import SampleFeatureChange, SampleState
from memes_search.application.ports.clock import IClock
from memes_search.application.ports.sample_admission import ISampleAdmission
from memes_search.application.ports.samples import ISampleRepository
from memes_search.application.ports.search_index import ISearchIndex


class ProjectionService:
    """Изолировать старый и новый индексы при параллельной перестройке."""

    def __init__(
        self,
        *,
        index: ISearchIndex,
        samples: ISampleRepository,
        admission: ISampleAdmission,
        clock: IClock,
        accepted_version: EmbeddingVersion,
    ) -> None:
        """Закрепить паспорт deployment до получения событий."""
        self._index = index
        self._samples = samples
        self._admission = admission
        self._clock = clock
        self._accepted_version = accepted_version

    async def public_feature(self, projection: FeatureProjection) -> bool:
        """Отклонить чужие embedding поколения, включая pending и failed."""
        if (
            projection.kind
            in {
                FeatureKind.VISUAL_EMBEDDING,
                FeatureKind.AUDIO_EMBEDDING,
                FeatureKind.TEXT_EMBEDDING,
            }
            and projection.embedding_version != self._accepted_version
        ):
            return False
        return await self._index.apply_feature(projection)

    async def private_feature(self, change: SampleFeatureChange) -> bool:
        """Принять только паспорт, закреплённый при создании образца."""
        if change.kind in {
            FeatureKind.VISUAL_EMBEDDING,
            FeatureKind.AUDIO_EMBEDDING,
            FeatureKind.TEXT_EMBEDDING,
        }:
            sample = await self._samples.get(change.sample_id)
            if sample is None or change.embedding_version != (
                sample.embedding_version or self._accepted_version
            ):
                return False
        applied = await self._samples.apply_features(change, self._clock.now())
        sample = await self._samples.get(change.sample_id)
        if sample is not None and (
            sample.state in {SampleState.READY, SampleState.FAILED, SampleState.CANCELLED}
            or sample.expires_at <= self._clock.now()
        ):
            await self._admission.release(sample.id)
        return applied
