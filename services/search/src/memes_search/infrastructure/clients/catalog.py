"""Авторитетная проверка активных медиа через Catalog REST."""

from datetime import datetime
from http import HTTPMethod, HTTPStatus
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from memes_kernel.values import FileRef, Sha256
from memes_platform.application.errors import ApplicationError
from memes_platform.infrastructure.http import InternalHttpClient
from memes_search.application.dto.projections import CatalogMedia
from memes_search.application.dto.queries import SearchLimit, SearchMediaKind
from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.application.ports.catalog import ICatalogReader
from memes_search.application.ports.inspection import IMediaInspector


class CatalogItem(BaseModel):
    """Типизированная форма ответа контролируемого ресурса."""

    media_id: UUID
    group_id: UUID
    group_revision: int
    composition_revision: int | None = None
    revision: int
    kind: SearchMediaKind
    published_at: datetime

    def dto(self) -> CatalogMedia:
        """Преобразовать транспорт в независимые прикладные данные."""
        return CatalogMedia(
            media_id=self.media_id,
            group_id=self.group_id,
            group_revision=self.composition_revision or self.group_revision,
            revision=self.revision,
            kind=self.kind,
            published_at=self.published_at,
        )


class CatalogBatch(BaseModel):
    """Ответ пакетного разрешения активных идентификаторов."""

    items: list[CatalogItem]


class InspectionResponse(BaseModel):
    """Проверенный декодером вид файла без публикации."""

    kind: SearchMediaKind


class CatalogRestReader(ICatalogReader, IMediaInspector):
    """HTTP-адаптер двух прикладных портов авторитетного Catalog."""

    def __init__(self, client: InternalHttpClient) -> None:
        """Сохранить общий ограниченный пул REST-соединений."""
        self._client = client

    async def resolve(self, media_ids: tuple[UUID, ...]) -> tuple[CatalogMedia, ...]:
        """Получить активные идентификаторы, составы и версии групп."""
        if not media_ids:
            return ()
        try:
            loaded: list[CatalogMedia] = []
            unique_ids = tuple(dict.fromkeys(media_ids))
            for offset in range(0, len(unique_ids), SearchLimit.CATALOG_BATCH_SIZE):
                chunk = unique_ids[offset : offset + SearchLimit.CATALOG_BATCH_SIZE]
                body = await self._client.request(
                    HTTPMethod.POST,
                    "/internal/media/resolve",
                    json={"media_ids": [str(identifier) for identifier in chunk]},
                )
                loaded.extend(item.dto() for item in CatalogBatch.model_validate(body).items)
            return tuple(loaded)
        except (ApplicationError, httpx.HTTPStatusError, ValidationError) as error:
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error

    async def by_hash(self, digest: Sha256) -> CatalogMedia | None:
        """Получить активное совпадение хэша либо отсутствие."""
        try:
            body = await self._client.request(
                HTTPMethod.GET, f"/internal/media/by-hash/{digest.value}"
            )
            return CatalogItem.model_validate(body).dto()
        except httpx.HTTPStatusError as error:
            if error.response.status_code == HTTPStatus.NOT_FOUND:
                return None
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error
        except (ApplicationError, ValidationError) as error:
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error

    async def inspect(self, key: str, file: FileRef) -> SearchMediaKind:
        """Использовать те же безопасные декодеры и лимиты, что публикации."""
        try:
            body = await self._client.request(
                HTTPMethod.POST,
                "/internal/media/inspect",
                json={
                    "object_key": key,
                    "file_id": str(file.id),
                    "sha256": file.sha256.value,
                    "size_bytes": file.size_bytes,
                },
            )
            return InspectionResponse.model_validate(body).kind
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {
                HTTPStatus.BAD_REQUEST,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            }:
                raise SearchError(SearchErrorCode.INVALID_SAMPLE) from error
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error
        except (ApplicationError, ValidationError) as error:
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error
