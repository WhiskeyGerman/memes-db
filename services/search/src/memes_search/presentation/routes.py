"""REST-контроллеры поиска, приватных образцов и проекций."""

from datetime import datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from memes_search.application.dto.identity import Principal
from memes_search.application.dto.queries import SearchLimit
from memes_search.application.dto.samples import PrivateSample
from memes_search.application.dto.sessions import SearchPage
from memes_search.presentation.container import Services
from memes_search.presentation.dependencies import (
    get_services,
    internal_access,
    optional_principal,
    required_owner,
)
from memes_search.presentation.links import SearchResourceLinks
from memes_search.presentation.sample_upload import UPLOAD_OPENAPI, sample_chunks
from memes_search.presentation.schemas.features import (
    FeatureDeliveryBody,
    MediaStateBody,
    SampleFeatureDeliveryBody,
)
from memes_search.presentation.schemas.search import SearchBody, SearchPageBody, SearchPendingBody

public = APIRouter(prefix="/v1/search", tags=["search"])
internal = APIRouter(
    prefix="/internal/search", tags=["internal"], dependencies=[Depends(internal_access)]
)


class ResourceHeader(StrEnum):
    """Закрепленный URI созданного ресурса."""

    LOCATION = "Location"


class SampleBody(BaseModel):
    """Образец без приватного S3-ключа и сырых признаков."""

    resource_url: str
    search_url: str
    download_url: str
    cancel_url: str
    sample_id: UUID
    state: str
    expires_at: str
    exact_media_id: UUID | None
    failure: str | None

    @classmethod
    def from_sample(cls, sample: PrivateSample, links: SearchResourceLinks) -> "SampleBody":
        """Сериализовать только доступное владельцу состояние."""
        return cls(
            resource_url=links.sample(sample.id),
            search_url=links.prefix,
            download_url=links.download(sample.id),
            cancel_url=links.sample(sample.id),
            sample_id=sample.id,
            state=sample.state,
            expires_at=sample.expires_at.isoformat(),
            exact_media_id=sample.exact_media_id,
            failure=sample.failure,
        )


@public.post("", response_model=SearchPageBody, responses={HTTPStatus.ACCEPTED: {}})
async def search(
    body: SearchBody,
    response: Response,
    services: Annotated[Services, Depends(get_services)],
    principal: Annotated[Principal | None, Depends(optional_principal)],
) -> SearchPageBody | JSONResponse:
    """Выполнить запрос или вернуть явное ожидание ML-признаков."""
    result = await services.search.execute(
        body.query(principal.user_id if principal else None), body.request_id or uuid4()
    )
    if isinstance(result, SearchPage):
        page = SearchPageBody.from_page(result, services.links)
        response.headers[ResourceHeader.LOCATION] = page.resource_url
        return page
    pending = SearchPendingBody.from_pending(result, services.links)
    return JSONResponse(
        status_code=HTTPStatus.ACCEPTED,
        content=pending.model_dump(mode="json"),
        headers={ResourceHeader.LOCATION: pending.resource_url},
    )


@public.get("/requests/{request_id}", response_model=SearchPageBody)
async def resume(
    request_id: UUID,
    response: Response,
    services: Annotated[Services, Depends(get_services)],
    principal: Annotated[Principal | None, Depends(optional_principal)],
) -> SearchPageBody | JSONResponse:
    """Продолжить сохранённый запрос после завершения inference."""
    result = await services.search.resume(request_id, principal.user_id if principal else None)
    if isinstance(result, SearchPage):
        page = SearchPageBody.from_page(result, services.links)
        response.headers[ResourceHeader.LOCATION] = page.resource_url
        return page
    pending = SearchPendingBody.from_pending(result, services.links)
    return JSONResponse(
        status_code=HTTPStatus.ACCEPTED,
        content=pending.model_dump(mode="json"),
        headers={ResourceHeader.LOCATION: pending.resource_url},
    )


@public.get("/sessions/{session_id}", response_model=SearchPageBody)
async def page(
    session_id: UUID,
    services: Annotated[Services, Depends(get_services)],
    principal: Annotated[Principal | None, Depends(optional_principal)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[
        int, Query(ge=1, le=SearchLimit.MAXIMUM_PAGE_SIZE)
    ] = SearchLimit.DEFAULT_PAGE_SIZE,
) -> SearchPageBody:
    """Выдать снимок только после текущей проверки групп и удалений."""
    return SearchPageBody.from_page(
        await services.search.page(
            session_id, principal.user_id if principal else None, offset, limit
        ),
        services.links,
    )


@public.post(
    "/samples",
    response_model=SampleBody,
    status_code=HTTPStatus.ACCEPTED,
    openapi_extra=UPLOAD_OPENAPI,
)
async def upload_sample(
    request: Request,
    response: Response,
    owner_id: Annotated[UUID, Depends(required_owner)],
    services: Annotated[Services, Depends(get_services)],
) -> SampleBody:
    """Загрузить приватный образец, не создающий публикацию."""
    chunks = sample_chunks(request)
    try:
        sample = SampleBody.from_sample(
            await services.samples.upload(owner_id, chunks), services.links
        )
        response.headers[ResourceHeader.LOCATION] = sample.resource_url
        return sample
    finally:
        await chunks.aclose()


@public.get("/samples/{sample_id}", response_model=SampleBody)
async def sample_state(
    sample_id: UUID,
    owner_id: Annotated[UUID, Depends(required_owner)],
    services: Annotated[Services, Depends(get_services)],
) -> SampleBody:
    """Прочитать только состояние собственного образца."""
    return SampleBody.from_sample(await services.samples.get(sample_id, owner_id), services.links)


@public.get("/samples/{sample_id}/download")
async def sample_download(
    sample_id: UUID,
    owner_id: Annotated[UUID, Depends(required_owner)],
    services: Annotated[Services, Depends(get_services)],
) -> dict[str, str]:
    """Получить короткую подписанную ссылку на собственный образец."""
    return {"url": await services.samples.download(sample_id, owner_id)}


@public.delete("/samples/{sample_id}", status_code=HTTPStatus.NO_CONTENT)
async def cancel_sample(
    sample_id: UUID,
    owner_id: Annotated[UUID, Depends(required_owner)],
    services: Annotated[Services, Depends(get_services)],
) -> None:
    """Сразу закрыть доступ к образцу и поставить отмену обработки."""
    await services.samples.cancel(sample_id, owner_id)


@internal.post("/features")
async def apply_feature(
    body: FeatureDeliveryBody,
    services: Annotated[Services, Depends(get_services)],
) -> dict[str, bool]:
    """Идемпотентно принять последнее поколение публичного признака."""
    return {"applied": await services.projections.public_feature(body.projection())}


@internal.post("/media-state")
async def apply_media_state(
    body: MediaStateBody,
    services: Annotated[Services, Depends(get_services)],
) -> dict[str, bool]:
    """Применить изменение состава или tombstone удалённого медиа."""
    return {"applied": await services.index.apply_media_state(body.change())}


@internal.post("/sample-features")
async def apply_sample_feature(
    body: SampleFeatureDeliveryBody,
    services: Annotated[Services, Depends(get_services)],
) -> dict[str, bool]:
    """Принять приватные признаки только живого, неотменённого образца."""
    return {"applied": await services.projections.private_feature(body.change())}


class SuggestionBody(BaseModel):
    """Параметры предложения вариаций."""

    media_id: UUID
    limit: int = Field(
        default=SearchLimit.VARIATION_SUGGESTIONS, ge=1, le=SearchLimit.VARIATION_SUGGESTIONS
    )


@internal.post("/suggestions")
async def suggestions(
    body: SuggestionBody,
    services: Annotated[Services, Depends(get_services)],
) -> dict[str, object]:
    """Вернуть совместимые активные группы кандидатов."""
    return {
        "items": jsonable_encoder(await services.suggestions.execute(body.media_id, body.limit))
    }


class AccountDeletionBody(BaseModel):
    """Сервисное событие удаления аккаунта для аудита доставки."""

    event_id: UUID
    occurred_at: str


@internal.post("/users/{user_id}/delete", dependencies=[Depends(internal_access)])
async def delete_private_data(
    user_id: UUID,
    body: AccountDeletionBody,
    services: Annotated[Services, Depends(get_services)],
) -> dict[str, bool]:
    """Стереть данные владельца только по аутентифицированному сервисному событию."""
    await services.delete_private_data.execute(user_id)
    return {"deleted": True}


class SampleAdmissionBody(BaseModel):
    """Минимальные метаданные общего server-controlled admission."""

    model_config = ConfigDict(extra="forbid")
    sample_id: UUID
    owner_id: UUID
    created_at: datetime
    expires_at: datetime


@internal.post("/sample-admissions", status_code=HTTPStatus.NO_CONTENT)
async def reserve_sample_admission(
    body: SampleAdmissionBody,
    services: Annotated[Services, Depends(get_services)],
) -> None:
    """Зарезервировать общий слот до чтения внешнего upload body."""
    await services.sample_admission.reserve(
        sample_id=body.sample_id,
        owner_id=body.owner_id,
        created_at=body.created_at,
        expires_at=body.expires_at,
    )


@internal.delete("/sample-admissions/{sample_id}", status_code=HTTPStatus.NO_CONTENT)
async def release_sample_admission(
    sample_id: UUID,
    services: Annotated[Services, Depends(get_services)],
) -> None:
    """Идемпотентно освободить терминальный admission резерв."""
    await services.sample_admission.release(sample_id)
