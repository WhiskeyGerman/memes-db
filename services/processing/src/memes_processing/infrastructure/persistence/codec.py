"""Сериализация прикладного состояния для PostgreSQL JSONB."""

from datetime import datetime
from uuid import UUID

from memes_kernel.values import FileRef, Sha256
from memes_processing.application.dto.coordinator import (
    CallbackTarget,
    MediaKind,
    Payload,
    Source,
    SourceType,
    WorkItem,
)
from memes_processing.application.dto.features import (
    integer,
    mapping,
    parse_version,
    sequence,
    string,
    version_payload,
)
from memes_processing.application.tasks import (
    AttemptLease,
    ProcessingTask,
    TaskFailure,
    TaskInput,
    TaskState,
)
from memes_processing.domain.features import CycleId, ExtractionId, FeatureKind


def optional_time(value: object) -> datetime | None:
    """Восстановить необязательное время."""
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def file_payload(file: FileRef) -> Payload:
    """Сериализовать ссылку на оригинал."""
    return {"id": str(file.id), "sha256": file.sha256.value, "size_bytes": file.size_bytes}


def parse_file(payload: Payload) -> FileRef:
    """Восстановить идентичность оригинала."""
    return FileRef(
        id=UUID(string(payload.get("id"))),
        sha256=Sha256(value=string(payload.get("sha256"))),
        size_bytes=integer(payload.get("size_bytes")),
    )


def source_payload(source: Source) -> Payload:
    """Сохранить оригинал без дублирования вычисленных результатов."""
    return {
        "id": str(source.id),
        "type": source.type.value,
        "kind": source.kind.value,
        "file": file_payload(source.file),
        "object_key": source.object_key,
        "created_at": source.created_at.isoformat(),
        "metadata": source.metadata,
        "expires_at": source.expires_at.isoformat() if source.expires_at else None,
        "deleted_at": source.deleted_at.isoformat() if source.deleted_at else None,
        "query_text": source.query_text,
        "revision": source.revision,
        "callback_target": source.callback_target.value,
        "embedding_version": version_payload(source.embedding_version)
        if source.embedding_version is not None
        else None,
    }


def parse_source(payload: Payload) -> Source:
    """Восстановить прикладное состояние оригинала."""
    text = payload.get("query_text")
    embedding_version = payload.get("embedding_version")
    return Source(
        id=UUID(string(payload.get("id"))),
        type=SourceType(string(payload.get("type"))),
        kind=MediaKind(string(payload.get("kind"))),
        file=parse_file(mapping(payload.get("file"))),
        object_key=string(payload.get("object_key")),
        created_at=datetime.fromisoformat(string(payload.get("created_at"))),
        metadata=mapping(payload.get("metadata")),
        expires_at=optional_time(payload.get("expires_at")),
        deleted_at=optional_time(payload.get("deleted_at")),
        query_text=string(text) if text is not None else None,
        revision=integer(payload.get("revision")),
        callback_target=CallbackTarget(
            string(payload.get("callback_target", CallbackTarget.PRIMARY))
        ),
        embedding_version=parse_version(mapping(embedding_version))
        if embedding_version is not None
        else None,
    )


def work_payload(work: WorkItem) -> Payload:
    """Сохранить задачу и fencing-контекст."""
    task = work.task
    lease: Payload | None = None
    if task.lease is not None:
        lease = {
            "id": str(task.lease.id),
            "worker_id": str(task.lease.worker_id),
            "started_at": task.lease.started_at.isoformat(),
            "expires_at": task.lease.expires_at.isoformat(),
            "deadline": task.lease.deadline.isoformat(),
        }
    return {
        "id": str(task.id),
        "extraction_id": str(task.input.extraction_id),
        "cycle_id": str(task.input.cycle_id),
        "kind": task.input.kind.value,
        "version": version_payload(task.input.version),
        "created_at": task.created_at.isoformat(),
        "state": task.state.value,
        "attempts": task.attempts,
        "lease": lease,
        "ready_at": task.ready_at.isoformat() if task.ready_at else None,
        "failure": task.failure.value if task.failure else None,
        "result": work.result,
        "accepted_attempt_id": str(work.accepted_attempt_id) if work.accepted_attempt_id else None,
        "accepted_worker_id": str(work.accepted_worker_id) if work.accepted_worker_id else None,
        "text_fragments": work.text_fragments,
        "generation": work.generation,
    }


def parse_work(payload: Payload, source: Source) -> WorkItem:
    """Восстановить задачу с ее актуальным оригиналом."""
    lease = None
    raw_lease = payload.get("lease")
    if raw_lease is not None:
        raw = mapping(raw_lease)
        lease = AttemptLease(
            id=UUID(string(raw.get("id"))),
            worker_id=UUID(string(raw.get("worker_id"))),
            started_at=datetime.fromisoformat(string(raw.get("started_at"))),
            expires_at=datetime.fromisoformat(string(raw.get("expires_at"))),
            deadline=datetime.fromisoformat(string(raw.get("deadline"))),
        )
    failure = payload.get("failure")
    result = payload.get("result")
    accepted = payload.get("accepted_attempt_id")
    worker = payload.get("accepted_worker_id")
    fragments = payload.get("text_fragments")
    return WorkItem(
        task=ProcessingTask(
            id=UUID(string(payload.get("id"))),
            input=TaskInput(
                extraction_id=ExtractionId(UUID(string(payload.get("extraction_id")))),
                cycle_id=CycleId(UUID(string(payload.get("cycle_id")))),
                source=source.file,
                kind=FeatureKind(string(payload.get("kind"))),
                version=parse_version(mapping(payload.get("version"))),
            ),
            created_at=datetime.fromisoformat(string(payload.get("created_at"))),
            state=TaskState(string(payload.get("state"))),
            attempts=integer(payload.get("attempts")),
            lease=lease,
            ready_at=optional_time(payload.get("ready_at")),
            failure=TaskFailure(string(failure)) if failure is not None else None,
        ),
        source=source,
        result=mapping(result) if result is not None else None,
        accepted_attempt_id=UUID(string(accepted)) if accepted is not None else None,
        accepted_worker_id=UUID(string(worker)) if worker is not None else None,
        text_fragments=sequence(fragments) if fragments is not None else None,
        generation=integer(payload.get("generation")),
    )
