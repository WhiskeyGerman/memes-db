"""REST-доставка одному независимо подтверждаемому получателю удаления."""

from enum import StrEnum
from http import HTTPStatus

import httpx

from memes_identity.application.dto.outbox import AccountDeletionDelivery, DeletionRecipient
from memes_identity.application.ports.outbox import IAccountDeletionPublisher
from memes_identity.infrastructure.configuration import IdentitySettings


class DeletionEndpoint(StrEnum):
    """Межсервисные контракты удаления без персональных данных."""

    CATALOG = "/internal/accounts/deleted"
    SEARCH = "/internal/search/users/{user_id}/delete"
    KEY_HEADER = "X-Internal-Key"


class RestAccountDeletionPublisher(IAccountDeletionPublisher):
    """Один HTTP-вызов; состояние частичной доставки сохраняет outbox."""

    def __init__(self, client: httpx.AsyncClient, settings: IdentitySettings) -> None:
        """Получить подключаемый transport и ключ текущего релиза.

        Args:
            client: HTTP-клиент процесса с конечным timeout.
            settings: Межсервисный ключ.
        """
        self._client = client
        self._settings = settings

    async def publish(self, delivery: AccountDeletionDelivery) -> bool:
        """Передать устойчивый ID единственному сохранённому получателю.

        Args:
            delivery: Независимая доставка.

        Returns:
            bool: Получатель подтвердил durable acceptance.
        """
        event = delivery.event
        payload: dict[str, str | bool] = {
            "event_id": str(event.event_id),
            "occurred_at": event.occurred_at.isoformat(),
        }
        if delivery.destination.recipient is DeletionRecipient.CATALOG:
            path: str = DeletionEndpoint.CATALOG
            payload.update(user_id=str(event.user_id), delete_media=event.delete_media)
        else:
            path = DeletionEndpoint.SEARCH.format(user_id=event.user_id)
        try:
            response = await self._client.post(
                f"{delivery.destination.base_url.rstrip('/')}{path}",
                headers={DeletionEndpoint.KEY_HEADER.value: self._settings.internal_api_key},
                json=payload,
            )
        except (httpx.RequestError, httpx.InvalidURL):
            return False
        return response.status_code in {HTTPStatus.OK, HTTPStatus.NO_CONTENT}
