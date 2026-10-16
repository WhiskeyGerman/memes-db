"""Композиция REST координатора; бизнес-правила находятся в Application."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hmac import compare_digest
from http import HTTPStatus
from random import SystemRandom
from typing import Annotated, cast
from uuid import UUID, uuid4

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import JsonValue

from memes_kernel.errors import DomainError
from memes_kernel.values import FileRef, Sha256
from memes_platform.infrastructure.database import create_engine, session_factory
from memes_platform.infrastructure.logging import ServiceName, configure_logging
from memes_platform.infrastructure.metrics import PostgreSQLMetrics
from memes_platform.presentation.body_limit import RequestBodyLimitMiddleware
from memes_platform.presentation.cors import install_cors
from memes_platform.presentation.observability import install_observability
from memes_processing.application.dto.coordinator import (
    CoordinatorCode,
    CoordinatorError,
    CoordinatorLimit,
    CoordinatorPolicy,
    Payload,
    Source,
    SourceType,
)
from memes_processing.application.dto.features import parse_version
from memes_processing.application.dto.query import QueryRequest
from memes_processing.application.ports.external import IArtifactStorage, IPreviewVerifier
from memes_processing.application.ports.identity import IIdentityGateway
from memes_processing.application.ports.repository import ITransaction
from memes_processing.application.tasks import SchedulingError
from memes_processing.application.use_cases.devices import Heartbeat, RegisterDevice, RevokeDevice
from memes_processing.application.use_cases.inspection import InspectSource, ListDevices
from memes_processing.application.use_cases.jobs import (
    CompleteProcessing,
    FailWork,
    PollWork,
    RenewWork,
)
from memes_processing.application.use_cases.permissions import AuthorizeOperator
from memes_processing.application.use_cases.queries import QueryStatus, RequestQuery
from memes_processing.application.use_cases.retry import RetryFeature
from memes_processing.application.use_cases.sources import (
    CoordinatorStatus,
    DeleteSource,
    PublishSource,
    SourceFeatures,
)
from memes_processing.domain.features import FeatureKind
from memes_processing.infrastructure.config.settings import ProcessingSettings
from memes_processing.infrastructure.http.identity import RESTIdentityGateway
from memes_processing.infrastructure.http.security import DeviceCredentials
from memes_processing.infrastructure.metrics import QUERIES
from memes_processing.infrastructure.persistence.repository import PostgresTransaction
from memes_processing.infrastructure.storage.preview import PillowPreviewVerifier
from memes_processing.infrastructure.storage.s3 import S3Artifacts
from memes_processing.presentation.schemas import (
    AttemptSchema,
    CompletionSchema,
    DeletedSchema,
    FailureSchema,
    PublishedSchema,
    QuerySchema,
    RegisterWorkerSchema,
    RetrySchema,
    SampleSchema,
    WorkerCapabilitiesSchema,
)


@dataclass(frozen=True, slots=True)
class Runtime:
    """Собранные в composition root зависимости процесса."""

    settings: ProcessingSettings
    transaction: ITransaction
    storage: IArtifactStorage
    client: httpx.AsyncClient
    policy: CoordinatorPolicy
    verifier: IPreviewVerifier
    identity: IIdentityGateway


def runtime(request: Request) -> Runtime:
    """Получить созданный lifespan контекст."""
    return cast(Runtime, request.app.state.runtime)


RuntimeDependency = Annotated[Runtime, Depends(runtime)]


def bearer(authorization: Annotated[str | None, Header()] = None) -> str:
    """Принять только индивидуальный Bearer токен."""
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=HTTPStatus.UNAUTHORIZED, detail="bearer_required")
    return authorization.removeprefix("Bearer ")


BearerDependency = Annotated[str, Depends(bearer)]


async def internal(
    context: RuntimeDependency,
    x_internal_key: Annotated[str | None, Header()] = None,
) -> None:
    """Проверить межсервисный секрет за постоянное время."""
    if x_internal_key is None or not compare_digest(
        x_internal_key, context.settings.internal_api_key
    ):
        raise HTTPException(
            status_code=HTTPStatus.FORBIDDEN, detail="internal_credentials_required"
        )


async def operator(context: RuntimeDependency, token: BearerDependency) -> None:
    """Сформировать прикладное разрешение из транспортных credentials."""
    await AuthorizeOperator(identity=context.identity, token=token).execute()


def create_app(settings: ProcessingSettings | None = None) -> FastAPI:
    """Создать приложение без подключения к ресурсам при импорте."""
    configured = settings or ProcessingSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(
            level=configured.log_level,
            release_id=configured.release_id,
            service_name=ServiceName.PROCESSING,
            environment=configured.deployment_environment,
        )
        engine = create_engine(
            configured.database_url,
            pool_size=configured.database_pool_size,
            max_overflow=configured.database_pool_overflow,
        )
        sessions = session_factory(engine)
        client = httpx.AsyncClient(timeout=configured.http_timeout_seconds)
        storage = S3Artifacts(configured)
        app.state.runtime = Runtime(
            settings=configured,
            transaction=PostgresTransaction(sessions),
            storage=storage,
            client=client,
            policy=configured.policy(),
            verifier=PillowPreviewVerifier(),
            identity=RESTIdentityGateway(client, configured),
        )
        app.state.internal_api_key = configured.internal_api_key
        app.state.observability.provider = PostgreSQLMetrics(engine, QUERIES)
        try:
            yield
        finally:
            await client.aclose()
            await storage.close()
            await engine.dispose()

    app = FastAPI(title="Memes processing", version=configured.release_id, lifespan=lifespan)
    app.add_middleware(RequestBodyLimitMiddleware)
    install_observability(app, service="processing")
    install_cors(app)

    @app.exception_handler(CoordinatorError)
    async def application_error(request: Request, error: CoordinatorError) -> JSONResponse:
        """Транспортный обработчик не меняет прикладные ошибки."""
        status_code = (
            HTTPStatus.UNAUTHORIZED
            if error.code is CoordinatorCode.UNAUTHORIZED
            else HTTPStatus.SERVICE_UNAVAILABLE
            if error.code is CoordinatorCode.UNAVAILABLE
            else HTTPStatus.UNPROCESSABLE_ENTITY
            if error.code is CoordinatorCode.INVALID_RESULT
            else HTTPStatus.NOT_FOUND
            if error.code is CoordinatorCode.NOT_FOUND
            else HTTPStatus.FORBIDDEN
            if error.code is CoordinatorCode.FORBIDDEN
            else HTTPStatus.CONFLICT
        )
        return JSONResponse(status_code=status_code, content={"detail": error.code.value})

    @app.exception_handler(SchedulingError)
    @app.exception_handler(DomainError)
    async def stale_result(request: Request, error: SchedulingError | DomainError) -> JSONResponse:
        """Просроченное назначение не превращается в серверный сбой."""
        return JSONResponse(status_code=HTTPStatus.CONFLICT, content={"detail": error.code.value})

    @app.get(response_model=dict[str, JsonValue], path="/health/live")
    async def liveness() -> Payload:
        """Процесс не объявляет готовность внешних ресурсов."""
        return {"status": "alive"}

    @app.get(response_model=dict[str, JsonValue], path="/health/ready")
    async def readiness(context: RuntimeDependency) -> Payload:
        """Проверить подключаемую БД без исполнения миграций."""
        await context.transaction.run(
            CoordinatorStatus(
                now=datetime.now(UTC),
                lease_seconds=context.settings.lease_seconds,
            )
        )
        return {"status": "ready"}

    @app.post(
        response_model=dict[str, JsonValue],
        path="/internal/media/published",
        dependencies=[Depends(internal)],
    )
    async def published(body: PublishedSchema, context: RuntimeDependency) -> Payload:
        """Принять публикацию и создать независимые циклы."""
        source = Source(
            id=body.media_id,
            type=SourceType.MEDIA,
            kind=body.media_kind,
            file=FileRef(
                id=body.file_id, sha256=Sha256(value=body.sha256), size_bytes=body.size_bytes
            ),
            object_key=body.object_key or f"originals/{body.media_id}/{body.file_id}",
            created_at=datetime.now(UTC),
            metadata=body.metadata,
            revision=body.source_revision,
        )
        return await context.transaction.run(
            PublishSource(event_id=body.event_id, source=source, policy=context.policy)
        )

    @app.post(
        response_model=dict[str, JsonValue],
        path="/internal/media/deleted",
        dependencies=[Depends(internal)],
    )
    async def deleted(body: DeletedSchema, context: RuntimeDependency) -> Payload:
        """Атомарно запретить результаты удаленного оригинала."""
        return await context.transaction.run(
            DeleteSource(event_id=body.event_id, source_id=body.media_id, now=datetime.now(UTC))
        )

    @app.get(
        response_model=dict[str, JsonValue],
        path="/internal/media/{media_id}/features",
        dependencies=[Depends(internal)],
    )
    async def features(media_id: UUID, context: RuntimeDependency) -> Payload:
        """Вернуть независимый прогресс публикации."""
        return await context.transaction.run(
            SourceFeatures(source_id=media_id, now=datetime.now(UTC))
        )

    @app.post(
        response_model=dict[str, JsonValue],
        path="/internal/processing/samples",
        dependencies=[Depends(internal)],
    )
    async def sample(body: SampleSchema, context: RuntimeDependency) -> Payload:
        """Создать приватный ограниченный по времени оригинал."""
        if body.expires_at.utcoffset() is None or body.expires_at <= datetime.now(UTC):
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail="sample_expired"
            )
        source = Source(
            id=body.sample_id,
            type=SourceType.SAMPLE,
            kind=body.media_kind,
            file=FileRef(
                id=body.file.id,
                sha256=Sha256(value=body.file.sha256),
                size_bytes=body.file.size_bytes,
            ),
            object_key=body.file.object_key or f"samples/{body.sample_id}/{body.file.id}",
            created_at=datetime.now(UTC),
            expires_at=body.expires_at,
            callback_target=body.callback_target,
            embedding_version=parse_version(
                cast(Payload, body.feature_version.model_dump(mode="json"))
            )
            if body.feature_version is not None
            else None,
            metadata=body.metadata,
        )
        await context.transaction.run(
            PublishSource(event_id=body.sample_id, source=source, policy=context.policy)
        )
        return {"sample_id": str(body.sample_id), "status": "pending"}

    @app.get(
        response_model=dict[str, JsonValue],
        path="/internal/processing/samples/{sample_id}/features",
        dependencies=[Depends(internal)],
    )
    async def sample_features(sample_id: UUID, context: RuntimeDependency) -> Payload:
        """Срок образца проверяется при каждом чтении."""
        return await context.transaction.run(
            SourceFeatures(source_id=sample_id, now=datetime.now(UTC))
        )

    @app.delete(
        response_model=dict[str, JsonValue],
        path="/internal/processing/samples/{sample_id}",
        dependencies=[Depends(internal)],
    )
    async def delete_sample(sample_id: UUID, context: RuntimeDependency) -> Payload:
        """Отменить задачи приватного образца."""
        return await context.transaction.run(
            DeleteSource(event_id=uuid4(), source_id=sample_id, now=datetime.now(UTC))
        )

    @app.post(
        response_model=dict[str, JsonValue],
        path="/internal/inference/query",
        dependencies=[Depends(internal)],
    )
    async def query(body: QuerySchema, context: RuntimeDependency) -> Payload:
        """Текст запроса исполняется реальной моделью доверенного воркера."""
        return await context.transaction.run(
            RequestQuery(
                request=QueryRequest(
                    request_id=body.request_id,
                    text=body.text,
                    space_id=body.space_id,
                    owner_id=body.owner_id,
                    feature_version=parse_version(
                        cast(Payload, body.feature_version.model_dump(mode="json"))
                    )
                    if body.feature_version is not None
                    else None,
                ),
                policy=context.policy,
                now=datetime.now(UTC),
            )
        )

    @app.delete(
        response_model=dict[str, JsonValue],
        path="/internal/inference/query/{task_id}",
        dependencies=[Depends(internal)],
    )
    async def cancel_query(task_id: UUID, context: RuntimeDependency) -> Payload:
        """Стереть приватный текст запроса и отменить его задачи."""
        return await context.transaction.run(
            DeleteSource(
                event_id=uuid4(),
                source_id=task_id,
                now=datetime.now(UTC),
            )
        )

    @app.delete(
        response_model=dict[str, JsonValue],
        path="/internal/inference/requests/{request_id}",
        dependencies=[Depends(internal)],
    )
    async def cancel_inference_request(request_id: UUID, context: RuntimeDependency) -> Payload:
        """Отменить запрос до назначения task, сохранив tombstone UUID."""
        return await cancel_query(request_id, context)

    @app.get(
        response_model=dict[str, JsonValue],
        path="/internal/inference/query/{task_id}",
        dependencies=[Depends(internal)],
    )
    async def query_status(task_id: UUID, context: RuntimeDependency) -> Payload:
        """Повторное чтение не продлевает приватный срок запроса."""
        return await context.transaction.run(QueryStatus(request_id=task_id, now=datetime.now(UTC)))

    @app.get("/v1/workers", response_model=dict[str, JsonValue], dependencies=[Depends(operator)])
    async def list_workers(context: RuntimeDependency) -> Payload:
        """Оператор видит устройства и текущие версии моделей без credentials."""
        return await context.transaction.run(ListDevices())

    @app.get(
        "/v1/processing/status",
        response_model=dict[str, JsonValue],
        dependencies=[Depends(operator)],
    )
    async def queue_status(context: RuntimeDependency) -> Payload:
        """Проверить глубину очередей и доступность доверенных устройств."""
        return await context.transaction.run(
            CoordinatorStatus(
                now=datetime.now(UTC),
                lease_seconds=context.settings.lease_seconds,
            )
        )

    @app.get(
        "/v1/processing/{source_id}",
        response_model=dict[str, JsonValue],
        dependencies=[Depends(operator)],
    )
    async def inspect_source(source_id: UUID, context: RuntimeDependency) -> Payload:
        """Показать историю независимых циклов, не раскрывая содержимое образца."""
        return await context.transaction.run(InspectSource(source_id=source_id))

    @app.post(
        response_model=dict[str, JsonValue],
        path="/v1/processing/{source_id}/retry",
        dependencies=[Depends(operator)],
    )
    async def retry(source_id: UUID, body: RetrySchema, context: RuntimeDependency) -> Payload:
        """Создать новый независимый цикл с возрастающим поколением."""
        return await context.transaction.run(
            RetryFeature(
                source_id=source_id,
                kind=FeatureKind(body.kind),
                feature_version=parse_version(
                    cast(Payload, body.feature_version.model_dump(mode="json"))
                )
                if body.feature_version is not None
                else None,
                policy=context.policy,
                now=datetime.now(UTC),
            )
        )

    @app.post(
        response_model=dict[str, JsonValue],
        path="/v1/workers",
        dependencies=[Depends(operator)],
        status_code=HTTPStatus.CREATED,
    )
    async def register(body: RegisterWorkerSchema, context: RuntimeDependency) -> Payload:
        """Оператор создает отзывные credentials конкретной машины."""
        return await context.transaction.run(
            RegisterDevice(name=body.name, credentials=DeviceCredentials(), now=datetime.now(UTC))
        )

    @app.delete(
        response_model=dict[str, JsonValue],
        path="/v1/workers/{worker_id}",
        dependencies=[Depends(operator)],
    )
    async def revoke(worker_id: UUID, context: RuntimeDependency) -> Payload:
        """После отзыва результаты устройства больше не принимаются."""
        return await context.transaction.run(
            RevokeDevice(worker_id=worker_id, now=datetime.now(UTC))
        )

    @app.post(response_model=dict[str, JsonValue], path="/v1/workers/{worker_id}/poll")
    async def poll(
        worker_id: UUID,
        body: WorkerCapabilitiesSchema,
        context: RuntimeDependency,
        token: BearerDependency,
    ) -> Payload:
        """Long polling не удерживает транзакцию между проверками очереди."""
        deadline = asyncio.get_running_loop().time() + context.settings.long_poll_seconds
        while True:
            result = await context.transaction.run(
                PollWork(
                    worker_id=worker_id,
                    token=token,
                    capabilities=cast(Payload, body.model_dump(mode="json")),
                    credentials=DeviceCredentials(),
                    storage=context.storage,
                    policy=context.policy,
                    now=datetime.now(UTC),
                )
            )
            if (
                result.get("assignment") is not None
                or asyncio.get_running_loop().time() >= deadline
            ):
                return result
            await asyncio.sleep(CoordinatorLimit.POLL_INTERVAL_MS / CoordinatorLimit.MILLISECONDS)

    @app.post(response_model=dict[str, JsonValue], path="/v1/workers/{worker_id}/heartbeat")
    async def heartbeat(
        worker_id: UUID,
        body: WorkerCapabilitiesSchema,
        context: RuntimeDependency,
        token: BearerDependency,
    ) -> Payload:
        """Ресурсы устройства доступны операторскому мониторингу."""
        return await context.transaction.run(
            Heartbeat(
                worker_id=worker_id,
                token=token,
                capabilities=cast(Payload, body.model_dump(mode="json")),
                credentials=DeviceCredentials(),
                now=datetime.now(UTC),
            )
        )

    @app.post(
        response_model=dict[str, JsonValue], path="/v1/workers/{worker_id}/tasks/{task_id}/renew"
    )
    async def renew(
        worker_id: UUID,
        task_id: UUID,
        body: AttemptSchema,
        context: RuntimeDependency,
        token: BearerDependency,
    ) -> Payload:
        """Продлить текущую попытку до ее абсолютного deadline."""
        return await context.transaction.run(
            RenewWork(
                worker_id=worker_id,
                token=token,
                task_id=task_id,
                attempt_id=body.attempt_id,
                credentials=DeviceCredentials(),
                policy=context.policy,
                now=datetime.now(UTC),
            )
        )

    @app.post(
        response_model=dict[str, JsonValue], path="/v1/workers/{worker_id}/tasks/{task_id}/complete"
    )
    async def complete(
        worker_id: UUID,
        task_id: UUID,
        body: CompletionSchema,
        context: RuntimeDependency,
        token: BearerDependency,
    ) -> Payload:
        """Координатор самостоятельно проверяет загруженный результат."""
        return await CompleteProcessing(
            worker_id=worker_id,
            token=token,
            task_id=task_id,
            attempt_id=body.attempt_id,
            checksum=body.sha256,
            credentials=DeviceCredentials(),
            verifier=context.verifier,
            policy=context.policy,
            now=datetime.now(UTC),
        ).execute(context.transaction, context.storage)

    @app.post(
        response_model=dict[str, JsonValue], path="/v1/workers/{worker_id}/tasks/{task_id}/fail"
    )
    async def fail(
        worker_id: UUID,
        task_id: UUID,
        body: FailureSchema,
        context: RuntimeDependency,
        token: BearerDependency,
    ) -> Payload:
        """Вернуть задачу в bounded retry либо закончить независимый этап."""
        return await context.transaction.run(
            FailWork(
                worker_id=worker_id,
                token=token,
                task_id=task_id,
                attempt_id=body.attempt_id,
                reason=body.reason,
                credentials=DeviceCredentials(),
                policy=context.policy,
                now=datetime.now(UTC),
                random_fraction=SystemRandom().random(),
            )
        )

    @app.get(
        response_model=dict[str, JsonValue],
        path="/internal/status",
        dependencies=[Depends(internal)],
    )
    async def status(context: RuntimeDependency) -> Payload:
        """Вернуть read model для admission без доступа к соседним таблицам."""
        return await context.transaction.run(
            CoordinatorStatus(
                now=datetime.now(UTC),
                lease_seconds=context.settings.lease_seconds,
            )
        )

    return app


def main() -> None:
    """Запустить самостоятельный HTTP процесс с конфигурацией окружения."""
    settings = ProcessingSettings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=settings.shutdown_seconds,
    )
