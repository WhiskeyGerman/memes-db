"""Операторский повтор создает новое поколение вместо перезаписи истории."""

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID, uuid4

from memes_kernel.values import Number
from memes_processing.application.dto.coordinator import (
    CoordinatorCode,
    CoordinatorError,
    CoordinatorPolicy,
    Payload,
    SourceType,
    WorkItem,
)
from memes_processing.application.ports.repository import IProcessingAction, IProcessingRepository
from memes_processing.application.tasks import ProcessingTask, TaskInput, TaskState
from memes_processing.application.use_cases.sources import notify, source_version
from memes_processing.application.use_cases.text_features import refresh_text_embedding
from memes_processing.domain.features import (
    CycleId,
    ExtractionId,
    FeatureKind,
    FeatureState,
    FeatureVersion,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryFeature(IProcessingAction):
    """Повтор одного этапа не отменяет успешные результаты других."""

    source_id: UUID
    kind: FeatureKind
    policy: CoordinatorPolicy
    now: datetime
    feature_version: FeatureVersion | None = None

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Атомарно отменить старую работу и поставить новый цикл."""
        source = await repository.source(self.source_id, lock=True)
        if source is None:
            raise CoordinatorError(CoordinatorCode.NOT_FOUND)
        if source.deleted_at is not None or (
            source.expires_at is not None and self.now >= source.expires_at
        ):
            raise CoordinatorError(CoordinatorCode.SOURCE_DELETED)
        pinned = source_version(source, self.kind, policy=self.policy)
        if (
            source.type is not SourceType.MEDIA
            and self.feature_version is not None
            and self.feature_version != pinned
        ):
            raise CoordinatorError(CoordinatorCode.WRONG_VERSION)
        selected = self.feature_version or pinned
        if self.feature_version is not None and (
            self.kind
            not in {
                FeatureKind.VISUAL_EMBEDDING,
                FeatureKind.AUDIO_EMBEDDING,
                FeatureKind.TEXT_EMBEDDING,
            }
            or selected.space is None
            or selected.dimensions is None
        ):
            raise CoordinatorError(CoordinatorCode.WRONG_VERSION)
        history = [
            work
            for work in await repository.source_tasks(source.id)
            if work.task.input.kind is self.kind
        ]
        if not history:
            raise CoordinatorError(CoordinatorCode.NOT_FOUND)
        for old in history:
            if old.task.lease is not None:
                await repository.attempt(old, state=TaskState.CANCELLED, finished_at=self.now)
            await repository.save_work(replace(old, task=old.task.cancel()))
        generation = max(work.generation for work in history) + Number.ONE
        task = ProcessingTask(
            id=uuid4(),
            created_at=self.now,
            input=TaskInput(
                extraction_id=ExtractionId(uuid4()),
                cycle_id=CycleId(uuid4()),
                source=source.file,
                kind=self.kind,
                version=selected,
            ),
        )
        work = WorkItem(
            task=task,
            source=source,
            generation=generation,
            text_fragments=history[-1].text_fragments,
        )
        await repository.save_work(work)
        await notify(repository, work, state=FeatureState.PENDING, now=self.now)
        await refresh_text_embedding(repository, work, policy=self.policy, now=self.now)
        return {
            "task_id": str(task.id),
            "cycle_id": str(task.input.cycle_id),
            "generation": generation,
        }
