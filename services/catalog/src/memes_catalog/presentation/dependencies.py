"""Сборка зависимостей HTTP на внешней границе Catalog."""

from dataclasses import dataclass
from enum import StrEnum
from secrets import compare_digest
from typing import Annotated, cast

from fastapi import Depends, Header, Request

from memes_catalog.application.dto.catalog import Actor
from memes_catalog.application.use_cases.catalog import CatalogUseCases
from memes_catalog.application.use_cases.variations import VariationUseCases
from memes_catalog.infrastructure.configuration import CatalogSettings
from memes_catalog.infrastructure.http.clients import IdentityClient, RestDelivery
from memes_catalog.infrastructure.persistence.catalog import PostgresCatalogFactory
from memes_catalog.infrastructure.storage.s3 import S3ObjectStorage
from memes_platform.application.errors import ApplicationError, ErrorCode


class HeaderName(StrEnum):
    """Имена заголовков границы доверия."""

    INTERNAL = "X-Internal-Key"
    AUTHORIZATION = "Authorization"
    BEARER = "Bearer"


@dataclass(frozen=True, slots=True)
class CatalogContainer:
    """Ресурсы, которыми владеет один HTTP-процесс."""

    settings: CatalogSettings
    factory: PostgresCatalogFactory
    storage: S3ObjectStorage
    identity: IdentityClient
    delivery: RestDelivery
    catalog: CatalogUseCases
    variations: VariationUseCases


def container(request: Request) -> CatalogContainer:
    """Получить ресурсы текущего процесса.

    Args:
        request: Запрос.
    """
    return cast(CatalogContainer, request.app.state.catalog)


async def actor(
    dependencies: Annotated[CatalogContainer, Depends(container)],
    authorization: Annotated[str | None, Header(alias=HeaderName.AUTHORIZATION)] = None,
) -> Actor:
    """Проверить Bearer в Identity перед изменением состояния.

    Args:
        dependencies: Ресурсы процесса.
        authorization: Внешний токен.
    """
    if authorization is None:
        raise ApplicationError(ErrorCode.UNAUTHORIZED)
    scheme, separator, token = authorization.partition(" ")
    if scheme != HeaderName.BEARER or not separator or not token:
        raise ApplicationError(ErrorCode.UNAUTHORIZED)
    return await dependencies.identity.authenticate(token)


def internal_access(
    dependencies: Annotated[CatalogContainer, Depends(container)],
    internal_key: Annotated[str | None, Header(alias=HeaderName.INTERNAL)] = None,
) -> None:
    """Проверить сервисный ключ постоянным по времени сравнением.

    Args:
        dependencies: Ресурсы процесса.
        internal_key: Предъявленный секрет.
    """
    if internal_key is None or not compare_digest(
        internal_key, dependencies.settings.internal_api_key
    ):
        raise ApplicationError(ErrorCode.UNAUTHORIZED)
