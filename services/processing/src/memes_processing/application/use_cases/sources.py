"""Независимая постановка этапов и отмена удаленных источников."""

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID, uuid4

from memes_processing.application.dto.coordinator import (
    CallbackTarget,
    CoordinatorCode,
    CoordinatorError,
    CoordinatorPolicy,
    EventDestination,
    JSONValue,
    MediaKind,
    OutgoingEvent,
    Payload,
    Source,
    SourceType,
    WorkItem,
)
from memes_processing.application.dto.features import version_payload
from memes_processing.application.ports.repository import IProcessingAction, IProcessingRepository
from memes_processing.application.tasks import ProcessingTask, TaskInput, TaskState
from memes_processing.domain.features import (
    CycleId,
    ExtractionId,
    FeatureKind,
    FeatureState,
    FeatureVersion,
)


async def notify(
    repository: IProcessingRepository, work: WorkItem, *, state: FeatureState, now: datetime
) -> None:
    """Добавить версионированное уведомление в ту же транзакцию."""
    if work.source.type is SourceType.QUERY:
        return
    event_id = uuid4()
    payload: Payload = {
        "event_id": str(event_id),
        "cycle_id": str(work.task.input.cycle_id),
        "generation": work.generation,
        "source_revision": work.source.revision,
        "kind": work.task.input.kind.value,
        "state": state.value,
        "feature_version": version_payload(work.task.input.version),
        "result": None if state is FeatureState.PENDING else work.result,
    }
    source_field = "media_id" if work.source.type is SourceType.MEDIA else "sample_id"
    payload[source_field] = str(work.source.id)
    if work.source.type is SourceType.SAMPLE:
        latest: dict[FeatureKind, WorkItem] = {}
        for candidate in (*await repository.source_tasks(work.source.id), work):
            if candidate.task.input.kind is FeatureKind.PREVIEW:
                continue
            previous = latest.get(candidate.task.input.kind)
            if previous is None or candidate.generation >= previous.generation:
                latest[candidate.task.input.kind] = candidate
        finished = bool(latest) and all(
            item.task.state
            in {
                TaskState.SUCCEEDED,
                TaskState.EMPTY,
                TaskState.FAILED,
                TaskState.CANCELLED,
            }
            for item in latest.values()
        )
        if finished:
            payload["sample_state"] = (
                "ready"
                if any(
                    item.task.state in {TaskState.SUCCEEDED, TaskState.EMPTY}
                    for item in latest.values()
                )
                else "failed"
            )
        else:
            payload["sample_state"] = "pending"
    await repository.enqueue(
        OutgoingEvent(
            id=event_id,
            destination=(
                EventDestination.MEDIA
                if work.source.type is SourceType.MEDIA
                else EventDestination.SHADOW_SAMPLE
                if work.source.callback_target is CallbackTarget.SHADOW
                else EventDestination.SAMPLE
            ),
            source_id=work.source.id,
            payload=payload,
            created_at=now,
        )
    )


def operations(kind: MediaKind) -> tuple[FeatureKind, ...]:
    """Выбрать независимые этапы для реальной модальности."""
    if kind is MediaKind.TEXT:
        return (FeatureKind.TEXT_EMBEDDING,)
    if kind is MediaKind.AUDIO:
        return (FeatureKind.TRANSCRIPT, FeatureKind.AUDIO_EMBEDDING)
    visual = (FeatureKind.PREVIEW, FeatureKind.OCR, FeatureKind.VISUAL_EMBEDDING)
    if kind is MediaKind.VIDEO:
        return (*visual, FeatureKind.TRANSCRIPT, FeatureKind.AUDIO_EMBEDDING)
    return visual


def source_version(
    source: Source, kind: FeatureKind, *, policy: CoordinatorPolicy
) -> FeatureVersion:
    """Приватный inference сохраняет выбранную версию до конца всех этапов."""
    embedding = source.embedding_version
    if embedding is not None:
        if (
            source.type is SourceType.MEDIA
            or embedding.space is None
            or embedding.dimensions is None
        ):
            raise CoordinatorError(CoordinatorCode.WRONG_VERSION)
        if kind in {
            FeatureKind.TEXT_EMBEDDING,
            FeatureKind.VISUAL_EMBEDDING,
            FeatureKind.AUDIO_EMBEDDING,
        }:
            return embedding
    return policy.versions[kind]


@dataclass(frozen=True, slots=True, kw_only=True)
class PublishSource(IProcessingAction):
    """Идемпотентно создать источник и задачи без расходования попыток."""

    event_id: UUID
    source: Source
    policy: CoordinatorPolicy

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Создать независимые циклы в одной транзакции."""
        if not await repository.claim_event(self.event_id):
            return {"source_id": str(self.source.id), "accepted": True}
        await repository.lock_source(self.source.id)
        if await repository.source_deleted(self.source.id):
            return {"source_id": str(self.source.id), "accepted": True, "cancelled": True}
        if self.source.callback_target not in self.policy.callback_targets:
            raise CoordinatorError(CoordinatorCode.UNAVAILABLE)
        if (
            self.source.type is not SourceType.SAMPLE
            and self.source.callback_target is not CallbackTarget.PRIMARY
        ):
            raise CoordinatorError(CoordinatorCode.WRONG_VERSION)
        existing = await repository.source(self.source.id, lock=True)
        if existing is not None:
            if existing.deleted_at is not None:
                raise CoordinatorError(CoordinatorCode.SOURCE_DELETED)
            if (
                existing.file != self.source.file
                or existing.revision != self.source.revision
                or existing.callback_target != self.source.callback_target
                or existing.embedding_version != self.source.embedding_version
                or existing.type != self.source.type
                or existing.kind != self.source.kind
            ):
                raise CoordinatorError(CoordinatorCode.SOURCE_CONFLICT)
            return {"source_id": str(existing.id), "accepted": True}
        await repository.save_source(self.source)
        task_ids: list[JSONValue] = []
        for kind in operations(self.source.kind):
            task = ProcessingTask(
                id=uuid4(),
                input=TaskInput(
                    extraction_id=ExtractionId(uuid4()),
                    cycle_id=CycleId(uuid4()),
                    source=self.source.file,
                    kind=kind,
                    version=source_version(self.source, kind, policy=self.policy),
                ),
                created_at=self.source.created_at,
            )
            work = WorkItem(task=task, source=self.source)
            await repository.save_work(work)
            await notify(repository, work, state=FeatureState.PENDING, now=self.source.created_at)
            task_ids.append(str(task.id))
        return {"source_id": str(self.source.id), "accepted": True, "task_ids": task_ids}


@dataclass(frozen=True, slots=True, kw_only=True)
class DeleteSource(IProcessingAction):
    """Удаление блокирует поздние результаты и новые назначения."""

    event_id: UUID
    source_id: UUID
    now: datetime

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Атомарно отменить все незавершенные этапы."""
        if not await repository.claim_event(self.event_id):
            return {"accepted": True}
        await repository.lock_source(self.source_id)
        source = await repository.source(self.source_id, lock=True)
        await repository.mark_deleted(self.source_id, now=self.now)
        if source is None:
            return {"accepted": True}
        if source.deleted_at is not None:
            return {"accepted": True}
        deleted = replace(
            source,
            deleted_at=self.now,
            revision=source.revision + 1,
            query_text=None,
            metadata={} if source.type is not SourceType.MEDIA else source.metadata,
        )
        await repository.save_source(deleted)
        for work in await repository.source_tasks(source.id):
            if work.task.lease is not None:
                await repository.attempt(work, state=TaskState.CANCELLED, finished_at=self.now)
            await repository.save_work(
                replace(
                    work,
                    task=work.task.cancel(),
                    source=deleted,
                    result=None,
                    text_fragments=None,
                )
            )
        return {"accepted": True}


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceFeatures(IProcessingAction):
    """Читать прогресс независимых этапов без обращений к другим сервисам."""

    source_id: UUID
    now: datetime

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Вернуть только актуальный и неистекший источник."""
        source = await repository.source(self.source_id)
        if source is None or source.deleted_at is not None:
            raise CoordinatorError(CoordinatorCode.NOT_FOUND)
        if source.expires_at is not None and self.now >= source.expires_at:
            raise CoordinatorError(CoordinatorCode.NOT_FOUND)
        features: list[JSONValue] = []
        for work in await repository.source_tasks(source.id):
            features.append(
                {
                    "task_id": str(work.task.id),
                    "cycle_id": str(work.task.input.cycle_id),
                    "kind": work.task.input.kind.value,
                    "generation": work.generation,
                    "state": work.task.state.value,
                    "feature_version": version_payload(work.task.input.version),
                    "result": work.result,
                }
            )
        return {"source_id": str(source.id), "features": features}


@dataclass(frozen=True, slots=True, kw_only=True)
class CoordinatorStatus(IProcessingAction):
    """Read model для admission и readiness."""

    now: datetime
    lease_seconds: int

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Прочитать только собственное состояние системы."""
        return await repository.status(now=self.now, lease_seconds=self.lease_seconds)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpirePrivateSources(IProcessingAction):
    """Удаление приватных данных не зависит от запросов пользователя."""

    now: datetime

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Отменить все этапы и стереть текст истекших поисковых источников."""
        sources = await repository.expired_sources(now=self.now)
        for source in sources:
            await DeleteSource(event_id=uuid4(), source_id=source.id, now=self.now).execute(
                repository
            )
        return {"expired_sources": len(sources)}
