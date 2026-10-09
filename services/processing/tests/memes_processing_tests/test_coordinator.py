import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from memes_processing.application.dto.coordinator import (
    CoordinatorCode,
    CoordinatorError,
    JSONValue,
    MediaKind,
    Payload,
    Source,
    SourceType,
)
from memes_processing.application.dto.features import mapping, sequence, string, version_payload
from memes_processing.application.dto.query import QueryRequest
from memes_processing.application.tasks import SchedulingError, TaskFailure, TaskState
from memes_processing.application.use_cases.devices import RegisterDevice, RevokeDevice
from memes_processing.application.use_cases.jobs import (
    CompleteWork,
    FailWork,
    PollWork,
    RecoverExpired,
)
from memes_processing.application.use_cases.queries import QueryStatus, RequestQuery
from memes_processing.application.use_cases.retry import RetryFeature
from memes_processing.application.use_cases.sources import (
    DeleteSource,
    ExpirePrivateSources,
    PublishSource,
    SourceFeatures,
)
from memes_processing.domain.features import FeatureKind, FeatureState
from memes_processing.infrastructure.persistence.models import (
    AttemptRow,
    OutboxRow,
    SourceRow,
    TaskRow,
)
from memes_processing.infrastructure.persistence.repository import PostgresRepository
from memes_processing_tests.fixtures import CoordinatorFixture


async def prepare(
    coordinator: CoordinatorFixture, source: Source, now: datetime
) -> tuple[UUID, str]:
    await coordinator.transaction.run(
        PublishSource(event_id=uuid4(), source=source, policy=coordinator.policy)
    )
    worker = await coordinator.transaction.run(
        RegisterDevice(name="integration-worker", credentials=coordinator.credentials, now=now)
    )
    return UUID(string(worker.get("worker_id"))), string(worker.get("token"))


async def poll(
    coordinator: CoordinatorFixture,
    worker_id: UUID,
    token: str,
    now: datetime,
    kind: FeatureKind = FeatureKind.OCR,
) -> Payload:
    result = await coordinator.transaction.run(
        PollWork(
            worker_id=worker_id,
            token=token,
            credentials=coordinator.credentials,
            capabilities={
                "maximum_parallel_tasks": 1,
                "versions": {kind.value: version_payload(coordinator.policy.versions[kind])},
            },
            policy=coordinator.policy,
            storage=coordinator.storage,
            now=now,
        )
    )
    return mapping(result.get("assignment"))


@pytest.mark.asyncio
async def test_publication_deduplicates_and_does_not_consume_attempts(
    coordinator: CoordinatorFixture, source: Source
) -> None:
    event_id = uuid4()
    await coordinator.transaction.run(
        PublishSource(event_id=event_id, source=source, policy=coordinator.policy)
    )
    await coordinator.transaction.run(
        PublishSource(event_id=event_id, source=source, policy=coordinator.policy)
    )
    async with coordinator.transaction.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TaskRow)) == 3
        assert await session.scalar(select(func.count()).select_from(OutboxRow)) == 3
        assert await session.scalar(select(func.count()).select_from(AttemptRow)) == 0
    progress = await coordinator.transaction.run(
        SourceFeatures(source_id=source.id, now=source.created_at)
    )
    assert all(
        mapping(feature).get("state") == TaskState.WAITING
        for feature in sequence(progress.get("features"))
    )


@pytest.mark.asyncio
async def test_acceptance_is_durable_and_duplicate_ack_does_not_emit_twice(
    coordinator: CoordinatorFixture, source: Source, now: datetime
) -> None:
    worker_id, token = await prepare(coordinator, source, now)
    assignment = await poll(coordinator, worker_id, token, now)
    task_id = UUID(string(assignment.get("task_id")))
    attempt_id = UUID(string(assignment.get("attempt_id")))
    raw = json.dumps(
        {
            "kind": "ocr",
            "feature_version": assignment["feature_version"],
            "result": {"fragments": [{"text": "мем meme", "language": "ru", "span": None}]},
        }
    ).encode()
    coordinator.storage.objects[string(assignment.get("artifact_key"))] = raw
    action = CompleteWork(
        worker_id=worker_id,
        token=token,
        task_id=task_id,
        attempt_id=attempt_id,
        checksum=sha256(raw).hexdigest(),
        credentials=coordinator.credentials,
        submitted_result=raw,
        policy=coordinator.policy,
        now=now,
    )
    first = await coordinator.transaction.run(action)
    second = await coordinator.transaction.run(action)
    assert first == second
    async with coordinator.transaction.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(OutboxRow)) == 5
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AttemptRow)
                .where(AttemptRow.state == TaskState.SUCCEEDED)
            )
            == 1
        )
    progress = await coordinator.transaction.run(SourceFeatures(source_id=source.id, now=now))
    assert [mapping(feature).get("state") for feature in sequence(progress.get("features"))].count(
        TaskState.SUCCEEDED
    ) == 1


@pytest.mark.asyncio
async def test_expired_fencing_id_cannot_replace_new_attempt(
    coordinator: CoordinatorFixture, source: Source, now: datetime
) -> None:
    worker_id, token = await prepare(coordinator, source, now)
    assigned = await poll(coordinator, worker_id, token, now)
    expired = now + coordinator.policy.lease_duration
    await coordinator.transaction.run(
        RecoverExpired(policy=coordinator.policy, now=expired, random_fraction=0.0)
    )
    renewed = await poll(
        coordinator, worker_id, token, expired + coordinator.policy.retries.base_delay
    )
    assert assigned["task_id"] == renewed["task_id"]
    assert assigned["attempt_id"] != renewed["attempt_id"]
    with pytest.raises(SchedulingError):
        await coordinator.transaction.run(
            CompleteWork(
                worker_id=worker_id,
                token=token,
                task_id=UUID(string(assigned.get("task_id"))),
                attempt_id=UUID(string(assigned.get("attempt_id"))),
                checksum="a" * 64,
                credentials=coordinator.credentials,
                submitted_result=b"",
                policy=coordinator.policy,
                now=expired + coordinator.policy.retries.base_delay,
            )
        )


@pytest.mark.asyncio
async def test_deleted_source_rejects_late_completion(
    coordinator: CoordinatorFixture, source: Source, now: datetime
) -> None:
    worker_id, token = await prepare(coordinator, source, now)
    assigned = await poll(coordinator, worker_id, token, now)
    await coordinator.transaction.run(DeleteSource(event_id=uuid4(), source_id=source.id, now=now))
    with pytest.raises(CoordinatorError) as error:
        await coordinator.transaction.run(
            CompleteWork(
                worker_id=worker_id,
                token=token,
                task_id=UUID(string(assigned.get("task_id"))),
                attempt_id=UUID(string(assigned.get("attempt_id"))),
                checksum="a" * 64,
                credentials=coordinator.credentials,
                submitted_result=b"",
                policy=coordinator.policy,
                now=now,
            )
        )
    assert error.value.code is CoordinatorCode.SOURCE_DELETED


@pytest.mark.asyncio
async def test_revoked_device_cannot_poll(
    coordinator: CoordinatorFixture, source: Source, now: datetime
) -> None:
    worker_id, token = await prepare(coordinator, source, now)
    await coordinator.transaction.run(RevokeDevice(worker_id=worker_id, now=now))
    with pytest.raises(CoordinatorError) as error:
        await poll(coordinator, worker_id, token, now)
    assert error.value.code is CoordinatorCode.FORBIDDEN


@pytest.mark.asyncio
async def test_operator_retry_creates_new_cycle_and_preserves_history(
    coordinator: CoordinatorFixture, source: Source, now: datetime
) -> None:
    await coordinator.transaction.run(
        PublishSource(event_id=uuid4(), source=source, policy=coordinator.policy)
    )
    response = await coordinator.transaction.run(
        RetryFeature(
            source_id=source.id,
            kind=FeatureKind.OCR,
            policy=coordinator.policy,
            now=now + timedelta(seconds=1),
        )
    )
    assert response["generation"] == 2
    async with coordinator.transaction.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TaskRow)) == 4
        assert (
            await session.scalar(
                select(func.count())
                .select_from(TaskRow)
                .where(TaskRow.state == TaskState.CANCELLED)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_incompatible_model_stays_waiting_without_attempt(
    coordinator: CoordinatorFixture, source: Source, now: datetime
) -> None:
    worker_id, token = await prepare(coordinator, source, now)
    wrong_version = replace(coordinator.policy.versions["ocr"], weights="incompatible")
    response = await coordinator.transaction.run(
        PollWork(
            worker_id=worker_id,
            token=token,
            credentials=coordinator.credentials,
            capabilities={
                "maximum_parallel_tasks": 1,
                "versions": {"ocr": version_payload(wrong_version)},
            },
            policy=coordinator.policy,
            storage=coordinator.storage,
            now=now,
        )
    )
    assert response["assignment"] is None
    async with coordinator.transaction.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AttemptRow)) == 0


@pytest.mark.asyncio
async def test_deletion_before_publication_is_durable(
    coordinator: CoordinatorFixture, source: Source, now: datetime
) -> None:
    await coordinator.transaction.run(DeleteSource(event_id=uuid4(), source_id=source.id, now=now))
    response = await coordinator.transaction.run(
        PublishSource(
            event_id=uuid4(),
            source=source,
            policy=coordinator.policy,
        )
    )
    assert response["cancelled"] is True
    async with coordinator.transaction.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TaskRow)) == 0
        assert await session.get(SourceRow, source.id) is None


@pytest.mark.asyncio
async def test_concurrent_publication_and_deletion_cannot_leave_active_work(
    coordinator: CoordinatorFixture, source: Source, now: datetime
) -> None:
    await asyncio.gather(
        coordinator.transaction.run(
            PublishSource(
                event_id=uuid4(),
                source=source,
                policy=coordinator.policy,
            )
        ),
        coordinator.transaction.run(
            DeleteSource(
                event_id=uuid4(),
                source_id=source.id,
                now=now,
            )
        ),
    )
    async with coordinator.transaction.sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(TaskRow)
                .where(
                    TaskRow.state.in_([TaskState.WAITING, TaskState.RUNNING]),
                )
            )
            == 0
        )


@pytest.mark.asyncio
async def test_equal_timestamps_do_not_deliver_result_before_pending(
    coordinator: CoordinatorFixture, source: Source, now: datetime
) -> None:
    worker_id, token = await prepare(coordinator, source, now)
    assignment = await poll(coordinator, worker_id, token, now)
    raw = json.dumps(
        {
            "kind": "ocr",
            "feature_version": assignment["feature_version"],
            "result": {"fragments": []},
        }
    ).encode()
    await coordinator.transaction.run(
        CompleteWork(
            worker_id=worker_id,
            token=token,
            task_id=UUID(string(assignment["task_id"])),
            attempt_id=UUID(string(assignment["attempt_id"])),
            checksum=sha256(raw).hexdigest(),
            credentials=coordinator.credentials,
            submitted_result=raw,
            policy=coordinator.policy,
            now=now,
        )
    )
    delivered = []
    for _ in range(4):
        async with coordinator.transaction.sessions() as session, session.begin():
            repository = PostgresRepository(session)
            events = await repository.outgoing(now=now)
            assert len(events) == 1
            delivered.append(events[0].payload["state"])
            await repository.acknowledge(events[0].id)
    assert delivered == ["pending", "pending", "pending", "empty"]


@pytest.mark.asyncio
async def test_query_expiry_erases_text_without_workers(
    coordinator: CoordinatorFixture, source: Source, now: datetime
) -> None:
    query = replace(
        source,
        type=SourceType.QUERY,
        kind=MediaKind.TEXT,
        query_text="private query",
        expires_at=now + timedelta(seconds=10),
        metadata={"owner_id": str(uuid4())},
    )
    await coordinator.transaction.run(
        PublishSource(
            event_id=uuid4(),
            source=query,
            policy=coordinator.policy,
        )
    )
    await coordinator.transaction.run(ExpirePrivateSources(now=now + timedelta(seconds=10)))
    async with coordinator.transaction.sessions() as session:
        row = await session.get(SourceRow, query.id)
        assert row is not None
        assert row.data["query_text"] is None
        assert row.data["metadata"] == {}
        tasks = (await session.scalars(select(TaskRow))).all()
        assert all(item.state == TaskState.CANCELLED for item in tasks)
        assert all(item.data["text_fragments"] is None for item in tasks)


@pytest.mark.asyncio
async def test_compatible_work_is_not_starved_by_101_old_model_tasks(
    coordinator: CoordinatorFixture, source: Source, now: datetime
) -> None:
    worker_id, token = await prepare(coordinator, source, now)
    async with coordinator.transaction.sessions() as session, session.begin():
        repository = PostgresRepository(session)
        tasks = await repository.source_tasks(source.id)
        current = next(item for item in tasks if item.task.input.kind is FeatureKind.OCR)
        old_input = replace(
            current.task.input,
            version=replace(current.task.input.version, weights="legacy-weights"),
        )
        for index in range(101):
            old_source = replace(source, id=uuid4())
            await repository.save_source(old_source)
            old_task = replace(
                current.task,
                id=uuid4(),
                input=old_input,
                created_at=now - timedelta(seconds=index + 1),
            )
            await repository.save_work(replace(current, task=old_task, source=old_source))
    assigned = await poll(coordinator, worker_id, token, now)
    assert assigned["source_id"] == str(source.id)
    assert assigned["feature_version"] == version_payload(coordinator.policy.versions["ocr"])


@pytest.mark.asyncio
async def test_query_reports_the_executed_passport_after_a_release_change(
    coordinator: CoordinatorFixture, now: datetime
) -> None:
    request = QueryRequest(
        request_id=uuid4(),
        text="private query",
        space_id="jina-v5-omni-nano",
    )
    pending = await coordinator.transaction.run(
        RequestQuery(request=request, policy=coordinator.policy, now=now)
    )
    assert pending["embedding"] is None
    async with coordinator.transaction.sessions() as session, session.begin():
        repository = PostgresRepository(session)
        tasks = await repository.source_tasks(request.request_id)
        work = tasks[0]
        original = replace(work.task.input.version, weights="previous-release-weights")
        assert original.dimensions is not None
        values: list[JSONValue] = [
            1.0 if index == 0 else 0.0 for index in range(original.dimensions)
        ]
        changed = replace(
            work,
            task=replace(
                work.task,
                input=replace(work.task.input, version=original),
                state=TaskState.SUCCEEDED,
                attempts=1,
            ),
            result={"items": [{"values": values, "span": None}]},
        )
        await repository.save_work(changed)
    response = await coordinator.transaction.run(
        QueryStatus(request_id=request.request_id, now=now)
    )
    embedding = mapping(response["embedding"])
    assert response["status"] == "succeeded"
    assert embedding["feature_version"] == version_payload(original)
    assert (
        mapping(embedding["feature_version"])["weights"]
        != coordinator.policy.versions["text_embedding"].weights
    )


@pytest.mark.asyncio
async def test_requested_query_passport_never_falls_back_to_current_workers(
    coordinator: CoordinatorFixture, now: datetime
) -> None:
    selected = replace(
        coordinator.policy.versions["text_embedding"],
        weights="old-release-for-rollback",
    )
    request = QueryRequest(
        request_id=uuid4(),
        text="private query",
        space_id="jina-v5-omni-nano",
        feature_version=selected,
    )
    await coordinator.transaction.run(
        RequestQuery(request=request, policy=coordinator.policy, now=now)
    )
    registered = await coordinator.transaction.run(
        RegisterDevice(name="current-worker", credentials=coordinator.credentials, now=now)
    )
    worker_id = UUID(string(registered["worker_id"]))
    token = string(registered["token"])
    response = await coordinator.transaction.run(
        PollWork(
            worker_id=worker_id,
            token=token,
            credentials=coordinator.credentials,
            capabilities={
                "maximum_parallel_tasks": 1,
                "versions": {
                    "text_embedding": version_payload(coordinator.policy.versions["text_embedding"])
                },
            },
            policy=coordinator.policy,
            storage=coordinator.storage,
            now=now,
        )
    )
    assert response["assignment"] is None
    async with coordinator.transaction.sessions() as session:
        repository = PostgresRepository(session)
        tasks = await repository.source_tasks(request.request_id)
        assert tasks[0].task.input.version == selected
        assert tasks[0].task.attempts == 0


@pytest.mark.asyncio
async def test_sample_preserves_requested_passport_for_later_ocr_text_embeddings(
    coordinator: CoordinatorFixture, source: Source, now: datetime
) -> None:
    selected = replace(
        coordinator.policy.versions["visual_embedding"],
        weights="old-release-for-rollback",
    )
    sample = replace(
        source,
        type=SourceType.SAMPLE,
        expires_at=now + timedelta(minutes=10),
        embedding_version=selected,
    )
    worker_id, token = await prepare(coordinator, sample, now)
    assigned = await poll(coordinator, worker_id, token, now)
    raw = json.dumps(
        {
            "kind": "ocr",
            "feature_version": assigned["feature_version"],
            "result": {"fragments": [{"text": "meme", "language": "en", "span": None}]},
        }
    ).encode()
    await coordinator.transaction.run(
        CompleteWork(
            worker_id=worker_id,
            token=token,
            task_id=UUID(string(assigned["task_id"])),
            attempt_id=UUID(string(assigned["attempt_id"])),
            checksum=sha256(raw).hexdigest(),
            credentials=coordinator.credentials,
            submitted_result=raw,
            policy=coordinator.policy,
            now=now,
        )
    )
    async with coordinator.transaction.sessions() as session:
        repository = PostgresRepository(session)
        tasks = await repository.source_tasks(sample.id)
        embeddings = [
            item
            for item in tasks
            if item.task.input.kind in {FeatureKind.VISUAL_EMBEDDING, FeatureKind.TEXT_EMBEDDING}
        ]
        assert len(embeddings) == 2
        assert all(item.task.input.version == selected for item in embeddings)


@pytest.mark.asyncio
async def test_public_retry_selects_new_embedding_passport_and_generation(
    coordinator: CoordinatorFixture, source: Source, now: datetime
) -> None:
    await prepare(coordinator, source, now)
    selected = replace(
        coordinator.policy.versions["visual_embedding"],
        weights="next-release-shadow",
    )
    response = await coordinator.transaction.run(
        RetryFeature(
            source_id=source.id,
            kind=FeatureKind.VISUAL_EMBEDDING,
            feature_version=selected,
            policy=coordinator.policy,
            now=now + timedelta(seconds=1),
        )
    )
    assert response["generation"] == 2
    async with coordinator.transaction.sessions() as session:
        repository = PostgresRepository(session)
        tasks = [
            work
            for work in await repository.source_tasks(source.id)
            if work.task.input.kind is FeatureKind.VISUAL_EMBEDDING
        ]
        old, new = tasks
        assert old.task.state is TaskState.CANCELLED
        assert old.task.input.version != selected
        assert new.task.state is TaskState.WAITING
        assert new.task.input.version == selected
        assert old.task.input.cycle_id != new.task.input.cycle_id
        events = (await session.scalars(select(OutboxRow).order_by(OutboxRow.sequence))).all()
        assert events[-1].payload["feature_version"] == version_payload(selected)
        assert events[-1].payload["state"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", tuple(MediaKind))
async def test_new_private_sample_emits_only_pending_notifications(
    coordinator: CoordinatorFixture, private_sample: Source, kind: MediaKind
) -> None:
    sample = replace(private_sample, kind=kind)
    await coordinator.transaction.run(
        PublishSource(event_id=uuid4(), source=sample, policy=coordinator.policy)
    )
    async with coordinator.transaction.sessions() as session:
        events = (
            await session.scalars(
                select(OutboxRow)
                .where(OutboxRow.source_id == sample.id)
                .order_by(OutboxRow.sequence)
            )
        ).all()
        tasks = (await session.scalars(select(TaskRow))).all()
        assert events
        assert all(item.state == TaskState.WAITING for item in tasks)
        assert all(item.payload["state"] == FeatureState.PENDING for item in events)
        assert all(item.payload["sample_state"] == "pending" for item in events)


@pytest.mark.asyncio
async def test_private_sample_terminal_notification_includes_dependent_text_embedding(
    coordinator: CoordinatorFixture,
    private_sample: Source,
    sample_feature_results: dict[FeatureKind, Payload],
    now: datetime,
) -> None:
    worker_id, token = await prepare(coordinator, private_sample, now)
    for kind, result in sample_feature_results.items():
        assigned = await poll(coordinator, worker_id, token, now, kind)
        raw = json.dumps(
            {"kind": kind.value, "feature_version": assigned["feature_version"], "result": result}
        ).encode()
        await coordinator.transaction.run(
            CompleteWork(
                worker_id=worker_id,
                token=token,
                task_id=UUID(string(assigned["task_id"])),
                attempt_id=UUID(string(assigned["attempt_id"])),
                checksum=sha256(raw).hexdigest(),
                credentials=coordinator.credentials,
                submitted_result=raw,
                policy=coordinator.policy,
                now=now,
            )
        )
    async with coordinator.transaction.sessions() as session:
        events = (await session.scalars(select(OutboxRow).order_by(OutboxRow.sequence))).all()
        assert all(item.payload["sample_state"] == "pending" for item in events[:-1])
        assert events[-1].payload["kind"] == FeatureKind.TEXT_EMBEDDING
        assert events[-1].payload["state"] == FeatureState.AVAILABLE
        assert events[-1].payload["sample_state"] == "ready"
        tasks = await PostgresRepository(session).source_tasks(private_sample.id)
        assert (
            next(item for item in tasks if item.task.input.kind is FeatureKind.PREVIEW).task.state
            is TaskState.WAITING
        )
        assert all(
            item.task.state is TaskState.SUCCEEDED
            for item in tasks
            if item.task.input.kind is not FeatureKind.PREVIEW
        )


@pytest.mark.asyncio
async def test_private_sample_is_failed_only_after_all_indexable_tasks_fail(
    coordinator: CoordinatorFixture, private_sample: Source, now: datetime
) -> None:
    worker_id, token = await prepare(coordinator, private_sample, now)
    for kind in (FeatureKind.OCR, FeatureKind.VISUAL_EMBEDDING):
        assigned = await poll(coordinator, worker_id, token, now, kind)
        await coordinator.transaction.run(
            FailWork(
                worker_id=worker_id,
                token=token,
                task_id=UUID(string(assigned["task_id"])),
                attempt_id=UUID(string(assigned["attempt_id"])),
                reason=TaskFailure.INVALID_INPUT,
                credentials=coordinator.credentials,
                policy=coordinator.policy,
                now=now,
                random_fraction=0.0,
            )
        )
    async with coordinator.transaction.sessions() as session:
        events = (await session.scalars(select(OutboxRow).order_by(OutboxRow.sequence))).all()
        assert all(item.payload["sample_state"] == "pending" for item in events[:-1])
        assert events[-1].payload["kind"] == FeatureKind.VISUAL_EMBEDDING
        assert events[-1].payload["state"] == FeatureState.FAILED
        assert events[-1].payload["sample_state"] == "failed"
