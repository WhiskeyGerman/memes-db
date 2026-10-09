"""REST-инференс настоящей модели через внешних исполнителей."""

import asyncio
from dataclasses import asdict
from http import HTTPMethod, HTTPStatus
from time import monotonic
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from memes_platform.application.errors import ApplicationError
from memes_platform.infrastructure.http import InternalHttpClient
from memes_search.application.dto.projections import EmbeddingVector, EmbeddingVersion
from memes_search.application.dto.samples import InferenceState, PrivateSample, QueryInference
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.application.ports.inference import IInferenceGateway


class InferenceVector(BaseModel):
    """Вектор из inference-пула с проверяемым пространством."""

    values: list[float]
    space_id: str
    feature_version: EmbeddingVersion


class InferenceResponse(BaseModel):
    """Ответ асинхронного вычисления текстового запроса."""

    task_id: UUID
    status: InferenceState
    embedding: InferenceVector | None = None

    def dto(self) -> QueryInference:
        """Отделить транспорт от результатов application."""
        return QueryInference(
            task_id=self.task_id,
            state=self.status,
            embedding=EmbeddingVector(
                values=tuple(self.embedding.values),
                space_id=self.embedding.space_id,
                version=self.embedding.feature_version,
            )
            if self.embedding is not None
            else None,
        )


class ProcessingRestGateway(IInferenceGateway):
    """Стабильные идентификаторы задач и ограниченное ожидание пула."""

    def __init__(
        self,
        client: InternalHttpClient,
        *,
        wait_seconds: float,
        poll_seconds: float,
        dimensions: int,
        space_id: str,
        version: EmbeddingVersion,
    ) -> None:
        """Задать REST-ресурс и максимальное ожидание query-inference."""
        self._client = client
        self._wait_seconds = wait_seconds
        self._poll_seconds = poll_seconds
        self._dimensions = dimensions
        self._space_id = space_id
        self._version = version

    async def query(
        self, request_id: UUID, text: str, space_id: str, owner_id: UUID | None
    ) -> QueryInference:
        """Вычислить вектор или вернуть явное ожидание без подмены."""
        try:
            body = await self._client.request(
                HTTPMethod.POST,
                "/internal/inference/query",
                json={
                    "request_id": str(request_id),
                    "text": text,
                    "space_id": space_id,
                    "owner_id": str(owner_id) if owner_id is not None else None,
                    "feature_version": asdict(self._version),
                },
            )
            result = InferenceResponse.model_validate(body).dto()
            if result.embedding is not None and (
                result.embedding.space_id != self._space_id
                or len(result.embedding.values) != self._dimensions
                or result.embedding.version != self._version
            ):
                raise SearchError(SearchErrorCode.UNAVAILABLE)
            deadline = monotonic() + self._wait_seconds
            while result.state is InferenceState.PENDING and monotonic() < deadline:
                await asyncio.sleep(min(self._poll_seconds, max(0.0, deadline - monotonic())))
                result = await self.query_result(result.task_id)
            return result
        except (ApplicationError, httpx.HTTPStatusError, ValidationError, SearchError) as error:
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error

    async def query_result(self, task_id: UUID) -> QueryInference:
        """Прочитать актуальный результат из персистентного координатора."""
        try:
            body = await self._client.request(
                HTTPMethod.GET, f"/internal/inference/query/{task_id}"
            )
            result = InferenceResponse.model_validate(body).dto()
            if result.embedding is not None and (
                result.embedding.space_id != self._space_id
                or len(result.embedding.values) != self._dimensions
                or result.embedding.version != self._version
            ):
                raise SearchError(SearchErrorCode.UNAVAILABLE)
            return result
        except (ApplicationError, httpx.HTTPStatusError, ValidationError, SearchError) as error:
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error

    async def submit_sample(self, sample: PrivateSample) -> None:
        """Передать приватный объект без создания публичного медиа."""
        if sample.file is None or sample.kind is None:
            raise SearchError(SearchErrorCode.INVALID_SAMPLE)
        try:
            await self._client.request(
                HTTPMethod.POST,
                "/internal/processing/samples",
                json={
                    "sample_id": str(sample.id),
                    "callback_target": sample.callback_target,
                    "feature_version": asdict(sample.embedding_version or self._version),
                    "owner_id": str(sample.owner_id),
                    "file": {
                        "id": str(sample.file.id),
                        "sha256": sample.file.sha256.value,
                        "size_bytes": sample.file.size_bytes,
                        "object_key": sample.object_key,
                    },
                    "media_kind": sample.kind,
                    "expires_at": sample.expires_at.isoformat(),
                },
            )
        except (ApplicationError, httpx.HTTPStatusError) as error:
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error

    async def cancel_sample(self, sample_id: UUID) -> None:
        """Отозвать задачи образца через идемпотентный REST."""
        try:
            await self._client.request(
                HTTPMethod.DELETE, f"/internal/processing/samples/{sample_id}"
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code == HTTPStatus.NOT_FOUND:
                return
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error
        except ApplicationError as error:
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error

    async def cancel_query(self, task_id: UUID) -> None:
        """Отозвать приватный query-inference и стереть его содержимое."""
        try:
            await self._client.request(HTTPMethod.DELETE, f"/internal/inference/query/{task_id}")
        except httpx.HTTPStatusError as error:
            if error.response.status_code == HTTPStatus.NOT_FOUND:
                return
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error
        except ApplicationError as error:
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error
