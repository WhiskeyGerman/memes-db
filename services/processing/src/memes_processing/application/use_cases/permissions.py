"""Операторское разрешение проверяется до изменения состояния."""

from dataclasses import dataclass, field

from memes_processing.application.dto.coordinator import CoordinatorCode, CoordinatorError
from memes_processing.application.dto.identity import PrincipalRole
from memes_processing.application.ports.identity import IIdentityGateway


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorizeOperator:
    """Роль является прикладным правилом, transport формирует лишь credentials."""

    identity: IIdentityGateway
    token: str = field(repr=False)

    async def execute(self) -> None:
        """Отклонить пользователя без операторского права."""
        principal = await self.identity.principal(self.token)
        if PrincipalRole.OPERATOR not in principal.roles:
            raise CoordinatorError(CoordinatorCode.FORBIDDEN)
