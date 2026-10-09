"""Защищённые REST-контракты между владельцами контекстов."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import JsonValue as ResponseJsonValue

from memes_catalog.application.dto.catalog import FeatureUpdate
from memes_catalog.application.dto.projections import media_payload
from memes_catalog.presentation.dependencies import CatalogContainer, container, internal_access
from memes_catalog.presentation.schemas.catalog import (
    DeletedAccountRequest,
    FeatureRequest,
    InspectRequest,
    ResolveRequest,
)
from memes_kernel.values import Sha256
from memes_platform.application.dto.json import JsonObject
from memes_platform.application.errors import ApplicationError, ErrorCode


class InternalPath(StrEnum):
    """Стабильные контракты каталоговых проекций."""

    RESOLVE = "/internal/media/resolve"
    HASH = "/internal/media/by-hash/{sha256}"
    INSPECT = "/internal/media/inspect"
    DELETED = "/internal/accounts/deleted"
    FEATURES = "/internal/media/{media_id}/features"


Dependencies = Annotated[CatalogContainer, Depends(container)]
router = APIRouter(dependencies=[Depends(internal_access)], tags=["internal"])


@router.post(InternalPath.RESOLVE, response_model=dict[str, ResponseJsonValue])
async def resolve(body: ResolveRequest, dependencies: Dependencies) -> JsonObject:
    """Получить живое членство для финальной проверки поиска."""
    visible = await dependencies.catalog.resolve(tuple(body.media_ids))
    identifiers = {view.media.id for view in visible}
    return {
        "items": [media_payload(view) for view in visible],
        "missing": [str(media_id) for media_id in body.media_ids if media_id not in identifiers],
    }


@router.get(InternalPath.HASH, response_model=dict[str, ResponseJsonValue])
async def by_hash(sha256: str, dependencies: Dependencies) -> JsonObject:
    """Найти существующее публичное медиа по точному хэшу."""
    return media_payload(await dependencies.catalog.by_hash(Sha256(value=sha256)))


@router.post(InternalPath.INSPECT, response_model=dict[str, ResponseJsonValue])
async def inspect(body: InspectRequest, dependencies: Dependencies) -> JsonObject:
    """Проверить приватный поисковый файл без публикации."""
    inspected = await dependencies.catalog.inspect_private(
        object_key=body.object_key, file_id=body.file_id, maximum_bytes=body.size_bytes
    )
    if inspected.original.file.sha256.value != body.sha256:
        raise ApplicationError(ErrorCode.INVALID_INPUT)
    return {
        "kind": inspected.original.kind.value,
        "format": inspected.original.format.value,
        "metadata": inspected.metadata,
        "sha256": inspected.original.file.sha256.value,
        "size_bytes": inspected.original.file.size_bytes,
    }


@router.post(InternalPath.DELETED, response_model=dict[str, ResponseJsonValue])
async def account_deleted(body: DeletedAccountRequest, dependencies: Dependencies) -> JsonObject:
    """Сохранить решение об удалении и немедленно запустить первый пакет очистки."""
    await dependencies.catalog.account_deleted(
        event_id=body.event_id,
        user_id=body.user_id,
        delete_media=body.delete_media,
        now=body.occurred_at,
    )
    return {"accepted": True}


@router.post(InternalPath.FEATURES, response_model=dict[str, ResponseJsonValue])
async def features(media_id: UUID, body: FeatureRequest, dependencies: Dependencies) -> JsonObject:
    """Принять результат текущего поколения источника."""
    if body.media_id is not None and body.media_id != media_id:
        raise ApplicationError(ErrorCode.INVALID_INPUT)
    update = FeatureUpdate(
        event_id=body.event_id,
        media_id=media_id,
        cycle_id=body.cycle_id,
        generation=body.generation,
        source_revision=body.source_revision,
        kind=body.kind,
        state=body.state,
        feature_version=body.feature_version,
        result=body.result,
    )
    accepted = await dependencies.catalog.accept_feature(update, now=datetime.now(UTC))
    return {"accepted": accepted}
