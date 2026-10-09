"""Межсервисная доставка outbox через REST."""

from http import HTTPStatus

import httpx

from memes_processing.application.dto.coordinator import EventDestination, OutgoingEvent
from memes_processing.application.ports.external import DeliveryUnavailableError, IEventDelivery
from memes_processing.infrastructure.config.settings import ProcessingSettings


class RESTDelivery(IEventDelivery):
    """Получатель dedup выполняет по event_id."""

    def __init__(self, client: httpx.AsyncClient, settings: ProcessingSettings) -> None:
        """Принять подключаемый транспорт и адреса сервисов."""
        self.client = client
        self.settings = settings

    async def deliver(self, event: OutgoingEvent) -> None:
        """Доставить событие, сохранив его исходный идентификатор."""
        if event.destination is EventDestination.MEDIA:
            url = (
                f"{self.settings.catalog_url.rstrip('/')}/internal/media/{event.source_id}/features"
            )
        else:
            base = (
                self.settings.shadow_search_url
                if event.destination is EventDestination.SHADOW_SAMPLE
                else self.settings.search_url
            )
            if not base:
                raise DeliveryUnavailableError("callback_target_unavailable")
            url = f"{base.rstrip('/')}/internal/search/sample-features"
        try:
            response = await self.client.post(
                url, json=event.payload, headers={"X-Internal-Key": self.settings.internal_api_key}
            )
            if response.status_code in {HTTPStatus.GONE, HTTPStatus.NOT_FOUND}:
                return
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise DeliveryUnavailableError("feature_delivery_unavailable") from error
