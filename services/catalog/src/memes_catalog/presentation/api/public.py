"""Публичные REST-маршруты Catalog."""

from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from pydantic import JsonValue as ResponseJsonValue

from memes_catalog.application.dto.catalog import (
    Actor,
    CatalogDefault,
    FeedCursor,
    MediaView,
    VoteOutcome,
)
from memes_catalog.application.dto.projections import media_payload
from memes_catalog.presentation.dependencies import CatalogContainer, actor, container
from memes_catalog.presentation.schemas.catalog import DismissRequest, MergeRequest, UploadRequest
from memes_platform.application.dto.json import JsonObject, JsonValue
from memes_platform.application.errors import ApplicationError, ErrorCode


class PublicPath(StrEnum):
    """Стабильные адреса публикаций и голосований."""

    UPLOADS = "/v1/uploads"
    UPLOAD = "/v1/uploads/{upload_id}"
    COMPLETE = "/v1/uploads/{upload_id}/complete"
    MEDIA = "/v1/media"
    ITEM = "/v1/media/{media_id}"
    GROUP = "/v1/groups/{group_id}"
    HISTORY = "/v1/groups/{group_id}/history"
    MERGE = "/v1/variations/merge"
    DETACH = "/v1/variations/memberships/{membership_id}/detach"
    WITHDRAW = "/v1/variations/ballots/{ballot_id}/vote"
    SUGGESTIONS = "/v1/media/{media_id}/suggestions"
    DISMISS = "/v1/media/{media_id}/suggestions/dismiss"


Dependencies = Annotated[CatalogContainer, Depends(container)]
CurrentActor = Annotated[Actor, Depends(actor)]
PageSize = Annotated[int, Query(ge=1, le=CatalogDefault.MAX_PAGE_SIZE)]
Offset = Annotated[int, Query(ge=0)]
router = APIRouter(tags=["catalog"])


def vote_payload(outcome: VoteOutcome) -> JsonObject:
    """Сериализовать идентичности голосования в JSON.

    Args:
        outcome: Прикладной результат.
    """
    return {
        "ballot_id": str(outcome.ballot_id),
        "active_votes": outcome.active_votes,
        "threshold": outcome.threshold,
        "applied": outcome.applied,
        "group_id": None if outcome.group_id is None else str(outcome.group_id),
    }


async def public_media(view: MediaView, dependencies: CatalogContainer) -> JsonObject:
    """Преобразовать снимок в публичный ответ без постоянного адреса S3.

    Args:
        view: Проверенный снимок.
        dependencies: Ресурсы.
    """
    author = view.author
    if author is None:
        raise ApplicationError(ErrorCode.UNAVAILABLE)
    features: list[JsonValue] = []
    for feature in view.features:
        projected: JsonObject = {"kind": feature["kind"], "state": feature["state"]}
        result = feature.get("result")
        if (
            feature.get("kind") == "preview"
            and isinstance(result, dict)
            and isinstance(result.get("object_key"), str)
        ):
            projected["url"] = await dependencies.storage.download_url(
                str(result["object_key"]), expires_seconds=CatalogDefault.DOWNLOAD_SECONDS
            )
        features.append(projected)
    data = media_payload(view)
    data.pop("author_id", None)
    data.update(
        {
            "author": {
                "user_id": None if author.login is None else str(author.user_id),
                "login": author.login,
                "display_name": author.login or "удаленный аккаунт",
            },
            "format": view.media.original.format.value,
            "metadata": view.metadata,
            "membership_id": str(view.membership.id),
            "url": await dependencies.storage.download_url(
                view.object_key, expires_seconds=CatalogDefault.DOWNLOAD_SECONDS
            ),
            "features": features,
        }
    )
    return data


@router.post(
    PublicPath.UPLOADS, response_model=dict[str, ResponseJsonValue], status_code=HTTPStatus.CREATED
)
async def begin_upload(
    body: UploadRequest, dependencies: Dependencies, current_actor: CurrentActor
) -> JsonObject:
    """Выдать ограниченную S3-форму передачи."""
    ticket = await dependencies.catalog.begin(
        actor=current_actor, size_bytes=body.size_bytes, now=datetime.now(UTC)
    )
    return {
        "upload_id": str(ticket.upload.id),
        "expires_at": ticket.upload.expires_at.isoformat(),
        "upload_url": ticket.url,
        "fields": dict(ticket.fields),
    }


@router.post(PublicPath.COMPLETE)
async def complete_upload(
    upload_id: UUID, dependencies: Dependencies, current_actor: CurrentActor
) -> JSONResponse:
    """Опубликовать файл либо вернуть ссылку на существующий мем."""
    outcome = await dependencies.catalog.complete(
        actor=current_actor, upload_id=upload_id, now=datetime.now(UTC)
    )
    return JSONResponse(
        status_code=HTTPStatus.CONFLICT if outcome.duplicate else HTTPStatus.CREATED,
        content={
            "media_id": str(outcome.media_id),
            "duplicate": outcome.duplicate,
            "url": f"/v1/media/{outcome.media_id}",
        },
    )


@router.delete(PublicPath.UPLOAD, status_code=HTTPStatus.NO_CONTENT)
async def cancel_upload(
    upload_id: UUID, dependencies: Dependencies, current_actor: CurrentActor
) -> Response:
    """Освободить незавершённую передачу."""
    await dependencies.catalog.cancel(actor=current_actor, upload_id=upload_id)
    return Response(status_code=HTTPStatus.NO_CONTENT)


@router.get(PublicPath.MEDIA, response_model=dict[str, ResponseJsonValue])
async def feed(
    dependencies: Dependencies,
    limit: PageSize = CatalogDefault.PAGE_SIZE,
    before: datetime | None = None,
    before_id: UUID | None = None,
) -> JsonObject:
    """Получить публичную ленту."""
    if before_id is not None and before is None:
        raise ApplicationError(ErrorCode.INVALID_INPUT)
    if before is not None and before.tzinfo is None:
        raise ApplicationError(ErrorCode.INVALID_INPUT)
    page = await dependencies.catalog.feed(
        before=None if before is None else FeedCursor(published_at=before, media_id=before_id),
        limit=limit,
    )
    return {
        "items": [await public_media(view, dependencies) for view in page.items],
        "next_before": None
        if page.next_cursor is None
        else page.next_cursor.published_at.isoformat(),
        "next_before_id": None if page.next_cursor is None else str(page.next_cursor.media_id),
    }


@router.get(PublicPath.ITEM, response_model=dict[str, ResponseJsonValue])
async def get_media(media_id: UUID, dependencies: Dependencies) -> JsonObject:
    """Получить публикацию и её текущую группу."""
    return await public_media(await dependencies.catalog.get(media_id), dependencies)


@router.delete(PublicPath.ITEM, status_code=HTTPStatus.NO_CONTENT)
async def delete_media(
    media_id: UUID, dependencies: Dependencies, current_actor: CurrentActor
) -> Response:
    """Удалить файл автора или исполнить операторское удаление."""
    await dependencies.catalog.delete(media_id=media_id, actor=current_actor, now=datetime.now(UTC))
    return Response(status_code=HTTPStatus.NO_CONTENT)


@router.get(PublicPath.GROUP, response_model=dict[str, ResponseJsonValue])
async def get_group(
    group_id: UUID,
    dependencies: Dependencies,
    offset: Offset = 0,
    limit: PageSize = CatalogDefault.PAGE_SIZE,
) -> JsonObject:
    """Открыть группу с учётом её преемника."""
    view = await dependencies.catalog.group(group_id=group_id, offset=offset, limit=limit)
    return {
        "group_id": str(view.group.id),
        "revision": view.group.revision.value,
        "composition_revision": view.group.composition_revision.value,
        "member_count": view.group.member_count,
        "cover_media_id": None if view.cover_media_id is None else str(view.cover_media_id),
        "items": [await public_media(item, dependencies) for item in view.items],
    }


@router.get(PublicPath.HISTORY, response_model=dict[str, ResponseJsonValue])
async def group_history(
    group_id: UUID,
    dependencies: Dependencies,
    limit: PageSize = CatalogDefault.PAGE_SIZE,
) -> JsonObject:
    """Получить обезличенную историю исполненных решений."""
    history = await dependencies.catalog.history(group_id=group_id, limit=limit)
    return {"items": list(history)}


@router.post(PublicPath.MERGE, response_model=dict[str, ResponseJsonValue])
async def merge(
    body: MergeRequest, dependencies: Dependencies, current_actor: CurrentActor
) -> JsonObject:
    """Подать уникальный голос за объединение составов."""
    result = await dependencies.variations.merge(
        actor=current_actor,
        first_id=body.first_id,
        second_id=body.second_id,
        first_revision=body.first_revision,
        second_revision=body.second_revision,
        now=datetime.now(UTC),
    )
    return vote_payload(result)


@router.post(PublicPath.DETACH, response_model=dict[str, ResponseJsonValue])
async def detach(
    membership_id: UUID, dependencies: Dependencies, current_actor: CurrentActor
) -> JsonObject:
    """Подать уникальный голос за открепление эпизода."""
    return vote_payload(
        await dependencies.variations.detach(
            actor=current_actor, membership_id=membership_id, now=datetime.now(UTC)
        )
    )


@router.delete(PublicPath.WITHDRAW, response_model=dict[str, ResponseJsonValue])
async def withdraw(
    ballot_id: UUID, dependencies: Dependencies, current_actor: CurrentActor
) -> JsonObject:
    """Отозвать голос до исполнения решения."""
    return vote_payload(
        await dependencies.variations.withdraw(
            actor=current_actor, ballot_id=ballot_id, now=datetime.now(UTC)
        )
    )


@router.get(PublicPath.SUGGESTIONS, response_model=dict[str, ResponseJsonValue])
async def suggestions(
    media_id: UUID,
    dependencies: Dependencies,
    current_actor: CurrentActor,
    limit: Annotated[
        int, Query(ge=1, le=CatalogDefault.MAX_SUGGESTIONS)
    ] = CatalogDefault.MAX_SUGGESTIONS,
) -> JsonObject:
    """Получить предложения похожих групп."""
    return await dependencies.catalog.suggestions(
        actor=current_actor, media_id=media_id, limit=limit
    )


@router.post(PublicPath.DISMISS, status_code=HTTPStatus.NO_CONTENT)
async def dismiss(
    media_id: UUID, body: DismissRequest, dependencies: Dependencies, current_actor: CurrentActor
) -> Response:
    """Скрыть личный кандидат для конкретного состава."""
    await dependencies.variations.dismiss(
        actor=current_actor,
        media_id=media_id,
        group_id=body.group_id,
        group_revision=body.group_revision,
        now=datetime.now(UTC),
    )
    return Response(status_code=HTTPStatus.NO_CONTENT)
