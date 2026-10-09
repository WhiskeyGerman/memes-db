"""HTTPS long polling и task-scoped загрузка результатов."""

import json
from datetime import datetime
from hashlib import sha256
from http import HTTPStatus
from uuid import UUID

import httpx

from memes_kernel.values import FileRef, Sha256
from memes_worker.application.dto.runtime import (
    Payload,
    WorkAssignment,
    WorkerFailure,
    WorkerTransportError,
    WorkResult,
)
from memes_worker.application.execution import Assignment, ExecutionKey, Operation
from memes_worker.application.ports.client import ICoordinatorClient
from memes_worker.infrastructure.config.settings import WorkerSettings
from memes_worker.infrastructure.wire import object_payload, value_integer, value_string


def parse_assignment(payload: Payload) -> WorkAssignment:
    """Восстановить контекст идентичности оригинала и версии."""
    file = object_payload(payload.get("file"))
    version = object_payload(payload.get("feature_version"))
    key = ExecutionKey(
        source_id=UUID(value_string(payload.get("source_id"))),
        source_hash=Sha256(value=value_string(file.get("sha256"))),
        cycle_id=UUID(value_string(payload.get("cycle_id"))),
        operation=Operation(value_string(payload.get("operation"))),
        fragment_id=UUID(value_string(payload.get("fragment_id"))),
        model_revision=value_string(version.get("weights")),
        preprocessing_revision=value_string(version.get("preprocessing")),
    )
    text = payload.get("query_text")
    return WorkAssignment(
        assignment=Assignment(
            task_id=UUID(value_string(payload.get("task_id"))),
            attempt_id=UUID(value_string(payload.get("attempt_id"))),
            source=FileRef(
                id=UUID(value_string(file.get("id"))),
                sha256=key.source_hash,
                size_bytes=value_integer(file.get("size_bytes")),
            ),
            key=key,
            lease_expires_at=datetime.fromisoformat(value_string(payload.get("lease_expires_at"))),
            deadline=datetime.fromisoformat(value_string(payload.get("deadline"))),
        ),
        source_type=value_string(payload.get("source_type")),
        media_kind=value_string(payload.get("media_kind")),
        feature_version=version,
        metadata=object_payload(payload.get("metadata")),
        download_url=value_string(payload.get("download_url")),
        upload_url=value_string(payload.get("upload_url")),
        preview_upload_url=value_string(payload.get("preview_upload_url")),
        preview_key=value_string(payload.get("preview_key")),
        query_text=value_string(text) if text is not None else None,
    )


class RESTCoordinatorClient(ICoordinatorClient):
    """Credentials устройства передаются только координатору."""

    def __init__(self, client: httpx.AsyncClient, settings: WorkerSettings) -> None:
        """Принять исходящий транспорт и индивидуальный секрет."""
        self.client = client
        self.settings = settings
        self.base = f"{settings.coordinator_url}/v1/workers/{settings.worker_id}"

    async def _request(self, endpoint: str, payload: Payload) -> Payload:
        """Классифицировать отзыв lease отдельно от временной сети."""
        try:
            response = await self.client.post(
                self.base + endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {self.settings.token}"},
            )
            if response.status_code in {
                HTTPStatus.UNAUTHORIZED,
                HTTPStatus.FORBIDDEN,
                HTTPStatus.CONFLICT,
                HTTPStatus.NOT_FOUND,
                HTTPStatus.GONE,
            }:
                raise WorkerTransportError(WorkerFailure.OWNERSHIP_LOST)
            if response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY:
                raise WorkerTransportError(WorkerFailure.INVALID_RESPONSE)
            response.raise_for_status()
            return object_payload(response.json())
        except (httpx.HTTPError, json.JSONDecodeError) as error:
            raise WorkerTransportError(WorkerFailure.TEMPORARY) from error

    async def poll(self, capabilities: Payload) -> WorkAssignment | None:
        """Дождаться задачи, не создавая входящего соединения."""
        data = await self._request("/poll", capabilities)
        assignment = data.get("assignment")
        if assignment is None:
            return None
        try:
            return parse_assignment(object_payload(assignment))
        except ValueError as error:
            raise WorkerTransportError(WorkerFailure.INVALID_RESPONSE) from error

    async def revocations(
        self, capabilities: Payload, source_ids: tuple[UUID, ...]
    ) -> tuple[UUID, ...]:
        """Heartbeat отзывает кэш, даже когда устройство не может принять работу."""
        data = await self._request(
            "/heartbeat",
            {
                **capabilities,
                "cached_source_ids": [str(item) for item in source_ids],
            },
        )
        raw = data.get("revoked_source_ids", [])
        if not isinstance(raw, list):
            raise WorkerTransportError(WorkerFailure.INVALID_RESPONSE)
        try:
            return tuple(UUID(value_string(item)) for item in raw)
        except ValueError as error:
            raise WorkerTransportError(WorkerFailure.INVALID_RESPONSE) from error

    async def renew(self, work: WorkAssignment) -> None:
        """Серверное время определяет действительность lease."""
        await self._request(
            f"/tasks/{work.assignment.task_id}/renew",
            {"attempt_id": str(work.assignment.attempt_id)},
        )

    async def submit(self, work: WorkAssignment, result: WorkResult) -> None:
        """Повторная приемка использует исходный fencing ID."""
        payload = dict(result.payload)
        if result.preview is not None:
            original = object_payload(payload.get("result"))
            original = dict(original)
            original["object_key"] = work.preview_key
            payload["result"] = original
        raw = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        try:
            if result.preview is not None:
                response = await self.client.put(
                    work.preview_upload_url,
                    content=result.preview,
                    headers={"Content-Type": "image/jpeg"},
                )
                response.raise_for_status()
            response = await self.client.put(
                work.upload_url, content=raw, headers={"Content-Type": "application/json"}
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise WorkerTransportError(WorkerFailure.TEMPORARY) from error
        await self._request(
            f"/tasks/{work.assignment.task_id}/complete",
            {"attempt_id": str(work.assignment.attempt_id), "sha256": sha256(raw).hexdigest()},
        )

    async def fail(self, work: WorkAssignment, reason: WorkerFailure) -> None:
        """В координатор не отправляется traceback либо пользовательский файл."""
        await self._request(
            f"/tasks/{work.assignment.task_id}/fail",
            {"attempt_id": str(work.assignment.attempt_id), "reason": reason.value},
        )
