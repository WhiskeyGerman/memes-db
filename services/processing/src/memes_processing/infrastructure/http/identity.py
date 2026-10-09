"""REST introspection Identity без утечки HTTP в прикладное разрешение."""

from http import HTTPStatus
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict

from memes_processing.application.dto.coordinator import CoordinatorCode, CoordinatorError
from memes_processing.application.dto.identity import Principal, PrincipalRole
from memes_processing.application.ports.identity import IIdentityGateway
from memes_processing.infrastructure.config.settings import ProcessingSettings


class IntrospectionSchema(BaseModel):
    """Ответ контрактного владельца доступа."""

    model_config = ConfigDict(extra="forbid")
    user_id: UUID
    login: str
    roles: frozenset[PrincipalRole]


class RESTIdentityGateway(IIdentityGateway):
    """Credentials сервиса передаются только Identity."""

    def __init__(self, client: httpx.AsyncClient, settings: ProcessingSettings) -> None:
        """Принять подключаемый backing service."""
        self.client = client
        self.endpoint = f"{settings.identity_url}/internal/auth/introspect"
        self.internal_key = settings.internal_api_key

    async def principal(self, token: str) -> Principal:
        """Транспортные ошибки отображаются в стабильную прикладную причину."""
        try:
            response = await self.client.post(
                self.endpoint,
                json={"token": token},
                headers={"X-Internal-Key": self.internal_key},
            )
            if response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
                raise CoordinatorError(CoordinatorCode.UNAUTHORIZED)
            response.raise_for_status()
            data = IntrospectionSchema.model_validate_json(response.content)
        except (httpx.HTTPError, ValueError) as error:
            if isinstance(error, CoordinatorError):
                raise
            raise CoordinatorError(CoordinatorCode.UNAVAILABLE) from error
        return Principal(id=data.user_id, roles=data.roles)
