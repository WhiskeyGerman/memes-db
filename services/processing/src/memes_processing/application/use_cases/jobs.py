"""Назначение, fencing, приемка и конечные повторы задач."""

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import sha256
from uuid import UUID, uuid4

from memes_processing.application.dto.coordinator import (
    CoordinatorCode,
    CoordinatorError,
    CoordinatorPolicy,
    Payload,
    SourceType,
    WorkerDevice,
    WorkItem,
)
from memes_processing.application.dto.features import (
    mapping,
    parse_result,
    parse_version,
    string,
    version_payload,
)
from memes_processing.application.ports.external import IArtifactStorage, IPreviewVerifier
from memes_processing.application.ports.repository import (
    IProcessingAction,
    IProcessingRepository,
    ITransaction,
)
from memes_processing.application.ports.security import IDeviceCredentials
from memes_processing.application.tasks import AttemptLease, TaskFailure, TaskState
from memes_processing.application.use_cases.sources import notify
from memes_processing.application.use_cases.text_features import refresh_text_embedding
from memes_processing.domain.features import FeatureExtraction, FeatureKind, FeatureState


async def authorize(
    repository: IProcessingRepository,
    *,
    worker_id: UUID,
    token: str,
    credentials: IDeviceCredentials,
    lock: bool = True,
) -> WorkerDevice:
    """Проверить индивидуальный секрет и отзыв устройства."""
    worker = await repository.worker(worker_id, lock=lock)
    if (
        worker is None
        or worker.revoked_at is not None
        or not credentials.verify(token, worker.token_hash)
    ):
        raise CoordinatorError(CoordinatorCode.FORBIDDEN)
    return worker


def require_source(work: WorkItem, *, now: datetime) -> None:
    """Запретить приемку удаленного либо истекшего оригинала."""
    if work.source.deleted_at is not None:
        raise CoordinatorError(CoordinatorCode.SOURCE_DELETED)
    if work.source.expires_at is not None and now >= work.source.expires_at:
        raise CoordinatorError(CoordinatorCode.SOURCE_DELETED)


def artifact_key(work: WorkItem, attempt_id: UUID) -> str:
    """Сформировать непересекающуюся область единственной попытки."""
    return f"attempts/{work.source.id}/{work.task.id}/{attempt_id}/result.json"


@dataclass(frozen=True, slots=True, kw_only=True)
class PollWork(IProcessingAction):
    """Выдать совместимую работу под блокировкой устройства."""

    worker_id: UUID
    token: str = field(repr=False)
    capabilities: Payload
    credentials: IDeviceCredentials
    storage: IArtifactStorage
    policy: CoordinatorPolicy
    now: datetime

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Назначить попытку и предоставить доступ только к ее объектам."""
        worker = await authorize(
            repository, worker_id=self.worker_id, token=self.token, credentials=self.credentials
        )
        await repository.save_worker(
            replace(worker, last_seen_at=self.now, capabilities=self.capabilities)
        )
        work = await repository.available(
            worker_id=worker.id, capabilities=self.capabilities, now=self.now
        )
        if work is None:
            return {"assignment": None}
        require_source(work, now=self.now)
        lease = AttemptLease(
            id=uuid4(),
            worker_id=worker.id,
            started_at=self.now,
            expires_at=self.now + self.policy.lease_duration,
            deadline=self.now + self.policy.attempt_duration,
        )
        assigned = replace(work, task=work.task.assign(lease=lease, now=self.now))
        await repository.save_work(assigned)
        await repository.attempt(assigned, state=TaskState.RUNNING)
        key = artifact_key(assigned, lease.id)
        preview_key = key.removesuffix("result.json") + "preview.jpg"
        access_expiry = lease.deadline + self.policy.lease_duration
        await repository.schedule_cleanup(key, ready_at=access_expiry)
        await repository.schedule_cleanup(preview_key, ready_at=access_expiry)
        assignment: Payload = {
            "task_id": str(work.task.id),
            "attempt_id": str(lease.id),
            "source_id": str(work.source.id),
            "source_type": work.source.type.value,
            "cycle_id": str(work.task.input.cycle_id),
            "fragment_id": str(work.task.id),
            "operation": work.task.input.kind.value,
            "feature_version": version_payload(work.task.input.version),
            "file": {
                "id": str(work.source.file.id),
                "sha256": work.source.file.sha256.value,
                "size_bytes": work.source.file.size_bytes,
            },
            "media_kind": work.source.kind.value,
            "metadata": {**work.source.metadata, "text_fragments": work.text_fragments},
            "query_text": work.source.query_text,
            "download_url": await self.storage.download_url(work.source.object_key)
            if work.source.type is not SourceType.QUERY
            else "",
            "upload_url": await self.storage.upload_url(key),
            "artifact_key": key,
            "preview_upload_url": await self.storage.upload_url(
                key.removesuffix("result.json") + "preview.jpg"
            ),
            "preview_key": key.removesuffix("result.json") + "preview.jpg",
            "lease_expires_at": lease.expires_at.isoformat(),
            "deadline": lease.deadline.isoformat(),
        }
        return {"assignment": assignment}


@dataclass(frozen=True, slots=True, kw_only=True)
class RenewWork(IProcessingAction):
    """Продлить право текущего исполнителя с серверным временем."""

    worker_id: UUID
    token: str = field(repr=False)
    task_id: UUID
    attempt_id: UUID
    credentials: IDeviceCredentials
    policy: CoordinatorPolicy
    now: datetime

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Проверить владельца, источник и предельный срок попытки."""
        await authorize(
            repository, worker_id=self.worker_id, token=self.token, credentials=self.credentials
        )
        work = await repository.work(self.task_id, lock=True)
        if work is None:
            raise CoordinatorError(CoordinatorCode.NOT_FOUND)
        require_source(work, now=self.now)
        lease = work.task.lease
        if lease is None or lease.worker_id != self.worker_id:
            raise CoordinatorError(CoordinatorCode.FORBIDDEN)
        renewed = lease.renew(
            attempt_id=self.attempt_id, now=self.now, duration=self.policy.lease_duration
        )
        await repository.save_work(replace(work, task=replace(work.task, lease=renewed)))
        return {
            "lease_expires_at": renewed.expires_at.isoformat(),
            "deadline": renewed.deadline.isoformat(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CompleteWork(IProcessingAction):
    """Принять результат только текущего цикла и попытки."""

    worker_id: UUID
    token: str = field(repr=False)
    task_id: UUID
    attempt_id: UUID
    checksum: str
    credentials: IDeviceCredentials
    submitted_result: bytes
    submitted_preview: bytes | None = None
    policy: CoordinatorPolicy
    now: datetime

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Проверить загруженные байты и атомарно отправить результат в outbox."""
        await authorize(
            repository, worker_id=self.worker_id, token=self.token, credentials=self.credentials
        )
        work = await repository.work(self.task_id, lock=True)
        if work is None:
            raise CoordinatorError(CoordinatorCode.NOT_FOUND)
        require_source(work, now=self.now)
        if work.accepted_attempt_id == self.attempt_id:
            if work.accepted_worker_id != self.worker_id:
                raise CoordinatorError(CoordinatorCode.FORBIDDEN)
            return {
                "accepted": True,
                "task_id": str(work.task.id),
                "attempt_id": str(self.attempt_id),
            }
        if work.task.lease is None or work.task.lease.worker_id != self.worker_id:
            raise CoordinatorError(CoordinatorCode.FORBIDDEN)
        work.task.lease.require_current(self.attempt_id, now=self.now)
        raw = self.submitted_result
        if len(raw) > self.policy.result_bytes:
            raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
        if sha256(raw).hexdigest() != self.checksum:
            raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
        try:
            decoded = mapping(json.loads(raw))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            raise CoordinatorError(CoordinatorCode.INVALID_RESULT) from error
        if string(decoded.get("kind")) != work.task.input.kind.value:
            raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
        try:
            version = parse_version(mapping(decoded.get("feature_version")))
            if version != work.task.input.version:
                raise CoordinatorError(CoordinatorCode.WRONG_VERSION)
            payload = mapping(decoded.get("result"))
            result = parse_result(work.task.input.kind, version, payload)
        except (ValueError, TypeError, KeyError) as error:
            raise CoordinatorError(CoordinatorCode.INVALID_RESULT) from error
        if work.task.input.kind is FeatureKind.PREVIEW:
            preview_key = string(payload.get("object_key"))
            expected_key = (
                artifact_key(work, self.attempt_id).removesuffix("result.json") + "preview.jpg"
            )
            if preview_key != expected_key:
                raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
            preview = self.submitted_preview
            if preview is None or len(preview) > self.policy.result_bytes:
                raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
            file = mapping(payload.get("file"))
            if sha256(preview).hexdigest() != string(file.get("sha256")) or len(
                preview
            ) != file.get("size_bytes"):
                raise CoordinatorError(CoordinatorCode.INVALID_RESULT)
        extraction = FeatureExtraction(
            id=work.task.input.extraction_id,
            media_id=work.source.id,
            source=work.source.file,
            cycle_id=work.task.input.cycle_id,
            kind=work.task.input.kind,
            version=version,
            requested_at=work.task.created_at,
        ).complete(cycle_id=work.task.input.cycle_id, result=result, now=self.now)
        await repository.attempt(work, state=TaskState.SUCCEEDED, finished_at=self.now)
        accepted = replace(
            work,
            task=work.task.accept(attempt_id=self.attempt_id, now=self.now),
            result=payload,
            accepted_attempt_id=self.attempt_id,
            accepted_worker_id=self.worker_id,
        )
        await repository.save_work(accepted)
        if work.task.input.kind is FeatureKind.PREVIEW and work.source.type is SourceType.MEDIA:
            await repository.retain_artifact(
                artifact_key(work, self.attempt_id).removesuffix("result.json") + "preview.jpg",
            )
        await notify(repository, accepted, state=extraction.state, now=self.now)
        await refresh_text_embedding(repository, accepted, policy=self.policy, now=self.now)
        return {"accepted": True, "task_id": str(work.task.id), "attempt_id": str(self.attempt_id)}


@dataclass(frozen=True, slots=True, kw_only=True)
class FailWork(IProcessingAction):
    """Зафиксировать неудачу с независимой политикой конечных повторов."""

    worker_id: UUID
    token: str = field(repr=False)
    task_id: UUID
    attempt_id: UUID
    reason: TaskFailure
    credentials: IDeviceCredentials
    policy: CoordinatorPolicy
    now: datetime
    random_fraction: float

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Не разрешить одному устройству завершить чужое назначение."""
        await authorize(
            repository, worker_id=self.worker_id, token=self.token, credentials=self.credentials
        )
        work = await repository.work(self.task_id, lock=True)
        if work is None:
            raise CoordinatorError(CoordinatorCode.NOT_FOUND)
        if work.task.lease is None or work.task.lease.worker_id != self.worker_id:
            raise CoordinatorError(CoordinatorCode.FORBIDDEN)
        failed = work.task.fail(
            attempt_id=self.attempt_id,
            reason=self.reason,
            policy=self.policy.retries,
            random_fraction=self.random_fraction,
            now=self.now,
        )
        await repository.attempt(work, state=failed.state, finished_at=self.now)
        changed = replace(work, task=failed)
        await repository.save_work(changed)
        if failed.state is TaskState.FAILED:
            await notify(repository, changed, state=FeatureState.FAILED, now=self.now)
            await refresh_text_embedding(repository, changed, policy=self.policy, now=self.now)
        return {"state": failed.state.value}


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoverExpired(IProcessingAction):
    """Восстановление после падения выполняется отдельным процессом."""

    policy: CoordinatorPolicy
    now: datetime
    random_fraction: float

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Истечение lease расходует попытку, отсутствие воркера - нет."""
        count = 0
        for work in await repository.expired(now=self.now):
            lease = work.task.lease
            if lease is None:
                continue
            await repository.attempt(work, state=TaskFailure.LEASE_EXPIRED, finished_at=self.now)
            if work.source.deleted_at is not None or (
                work.source.expires_at is not None and self.now >= work.source.expires_at
            ):
                updated = work.task.cancel()
            else:
                updated = work.task.fail(
                    attempt_id=lease.id,
                    reason=TaskFailure.LEASE_EXPIRED,
                    policy=self.policy.retries,
                    random_fraction=self.random_fraction,
                    now=self.now,
                )
            changed = replace(work, task=updated)
            await repository.save_work(changed)
            if updated.state is TaskState.FAILED:
                await notify(repository, changed, state=FeatureState.FAILED, now=self.now)
            await refresh_text_embedding(repository, changed, policy=self.policy, now=self.now)
            count += 1
        return {"recovered": count}


@dataclass(frozen=True, slots=True, kw_only=True)
class CompletionPreflight(IProcessingAction):
    """Проверка доступа до сетевого чтения без длительного row lock."""

    worker_id: UUID
    token: str = field(repr=False)
    task_id: UUID
    attempt_id: UUID
    credentials: IDeviceCredentials
    now: datetime

    async def execute(self, repository: IProcessingRepository) -> Payload:
        """Получить только разрешенные объекты актуальной попытки."""
        await authorize(
            repository,
            worker_id=self.worker_id,
            token=self.token,
            credentials=self.credentials,
            lock=False,
        )
        work = await repository.work(self.task_id)
        if work is None:
            raise CoordinatorError(CoordinatorCode.NOT_FOUND)
        require_source(work, now=self.now)
        if work.accepted_attempt_id == self.attempt_id:
            if work.accepted_worker_id != self.worker_id:
                raise CoordinatorError(CoordinatorCode.FORBIDDEN)
            return {"already_accepted": True}
        lease = work.task.lease
        if lease is None or lease.worker_id != self.worker_id:
            raise CoordinatorError(CoordinatorCode.FORBIDDEN)
        lease.require_current(self.attempt_id, now=self.now)
        key = artifact_key(work, self.attempt_id)
        return {
            "already_accepted": False,
            "artifact_key": key,
            "preview_key": key.removesuffix("result.json") + "preview.jpg",
            "is_preview": work.task.input.kind is FeatureKind.PREVIEW,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CompleteProcessing:
    """Сетевые ресурсы читаются вне транзакционной блокировки удаления."""

    worker_id: UUID
    token: str = field(repr=False)
    task_id: UUID
    attempt_id: UUID
    checksum: str
    credentials: IDeviceCredentials
    verifier: IPreviewVerifier
    policy: CoordinatorPolicy
    now: datetime

    async def execute(self, transaction: ITransaction, storage: IArtifactStorage) -> Payload:
        """После чтения повторно проверить fencing и tombstone в транзакции."""
        preflight = await transaction.run(
            CompletionPreflight(
                worker_id=self.worker_id,
                token=self.token,
                task_id=self.task_id,
                attempt_id=self.attempt_id,
                credentials=self.credentials,
                now=self.now,
            )
        )
        raw = b""
        preview = None
        if not preflight.get("already_accepted"):
            raw = await storage.result(
                string(preflight.get("artifact_key")), maximum_bytes=self.policy.result_bytes
            )
            if preflight.get("is_preview"):
                preview = await storage.result(
                    string(preflight.get("preview_key")), maximum_bytes=self.policy.result_bytes
                )
                try:
                    await self.verifier.verify(preview)
                except ValueError as error:
                    raise CoordinatorError(CoordinatorCode.INVALID_RESULT) from error
        return await transaction.run(
            CompleteWork(
                worker_id=self.worker_id,
                token=self.token,
                task_id=self.task_id,
                attempt_id=self.attempt_id,
                checksum=self.checksum,
                credentials=self.credentials,
                submitted_result=raw,
                submitted_preview=preview,
                policy=self.policy,
                now=datetime.now(self.now.tzinfo),
            )
        )
