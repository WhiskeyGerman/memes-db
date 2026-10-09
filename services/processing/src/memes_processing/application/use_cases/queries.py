"""Приватное query inference с версией реально исполненного задания."""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from uuid import UUID

from memes_kernel.values import FileRef, Number, Sha256
from memes_processing.application.dto.coordinator import (
    CoordinatorCode,
    CoordinatorError,
    CoordinatorPolicy,
    MediaKind,
    Payload,
    Source,
    SourceType,
)
from memes_processing.application.dto.features import mapping, sequence, version_payload
from memes_processing.application.dto.query import QueryRequest, QueryState
from memes_processing.application.ports.repository import IProcessingAction, IProcessingRepository
from memes_processing.application.tasks import TaskState
from memes_processing.application.use_cases.sources import PublishSource
from memes_processing.domain.features import FeatureKind


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestQuery(IProcessingAction):
    """Поставить текст в очередь и прочитать сохранённый исходный паспорт."""

    request: QueryRequest
    policy: CoordinatorPolicy
    now: datetime

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Срок не продлевается при повторном request UUID."""
        version = self.request.feature_version or self.policy.versions[FeatureKind.TEXT_EMBEDDING]
        if (
            self.request.space_id != version.space
            or version.space is None
            or version.dimensions is None
        ):
            raise CoordinatorError(CoordinatorCode.WRONG_VERSION)
        encoded = self.request.text.encode()
        source = Source(
            id=self.request.request_id,
            type=SourceType.QUERY,
            kind=MediaKind.TEXT,
            file=FileRef(
                id=self.request.request_id,
                sha256=Sha256(value=sha256(encoded).hexdigest()),
                size_bytes=len(encoded),
            ),
            object_key="",
            created_at=self.now,
            expires_at=self.now + self.policy.attempt_duration,
            query_text=self.request.text,
            embedding_version=version,
            metadata={"owner_id": str(self.request.owner_id)} if self.request.owner_id else {},
        )
        await PublishSource(
            event_id=self.request.request_id, source=source, policy=self.policy
        ).execute(repository)
        return await QueryStatus(request_id=source.id, now=self.now).execute(repository)


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryStatus(IProcessingAction):
    """Результат всегда ссылается на паспорт сохранённой задачи."""

    request_id: UUID
    now: datetime

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Не применять текущие settings к вектору предыдущей ревизии."""
        source = await repository.source(self.request_id)
        if (
            source is None
            or source.type is not SourceType.QUERY
            or source.deleted_at is not None
            or (source.expires_at is not None and self.now >= source.expires_at)
        ):
            raise CoordinatorError(CoordinatorCode.NOT_FOUND)
        candidates = [
            item
            for item in await repository.source_tasks(source.id)
            if item.task.input.kind is FeatureKind.TEXT_EMBEDDING
        ]
        if not candidates:
            raise CoordinatorError(CoordinatorCode.NOT_FOUND)
        work = max(candidates, key=lambda item: item.generation)
        embedding: Payload | None = None
        state = QueryState.PENDING
        if work.task.state in {TaskState.FAILED, TaskState.CANCELLED, TaskState.EMPTY}:
            state = QueryState.FAILED
        elif work.task.state is TaskState.SUCCEEDED:
            items = sequence(mapping(work.result).get("items"))
            if items:
                embedding = {
                    "values": mapping(items[Number.ZERO]).get("values"),
                    "space_id": work.task.input.version.space,
                    "feature_version": version_payload(work.task.input.version),
                }
                state = QueryState.SUCCEEDED
            else:
                state = QueryState.FAILED
        return {
            "task_id": str(self.request_id),
            "status": state.value,
            "embedding": embedding,
        }
