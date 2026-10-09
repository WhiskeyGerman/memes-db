"""REST доступ shadow Search к primary admission resource."""

from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPMethod, HTTPStatus
from uuid import UUID, uuid4

import httpx

from memes_search.application.errors import SearchError, SearchErrorCode
from memes_search.application.ports.sample_admission import ISampleAdmission


class AdmissionPath(StrEnum):
    """Только разрешённые межсервисные операции общего ledger."""

    RESERVE = "/internal/search/sample-admissions"
    RELEASE = "/internal/search/sample-admissions/{sample_id}"
    DELETE_OWNER = "/internal/search/users/{owner_id}/delete"
    INTERNAL_KEY = "X-Internal-Key"


class RestSampleAdmission(ISampleAdmission):
    """Fail-closed HTTP адаптер; секрет и primary URL получены из release config."""

    def __init__(
        self,
        *,
        base_url: str,
        internal_api_key: str,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Создать ограниченный транспорт без перенаправлений.

        Args:
            base_url: Allowlisted primary admission URL.
            internal_api_key: Межсервисный секрет.
            timeout: Конечный транспортный timeout.
            transport: Изолированный test transport.
        """
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            follow_redirects=False,
            headers={AdmissionPath.INTERNAL_KEY.value: internal_api_key},
            transport=transport,
        )

    async def reserve(
        self, *, sample_id: UUID, owner_id: UUID, created_at: datetime, expires_at: datetime
    ) -> None:
        """Попросить primary применить его собственную quota/TTL policy.

        Args:
            sample_id: ID образца.
            owner_id: Проверенный владелец.
            created_at: Время создания.
            expires_at: Конечный срок.
        """
        await self._request(
            HTTPMethod.POST,
            AdmissionPath.RESERVE,
            {
                "sample_id": str(sample_id),
                "owner_id": str(owner_id),
                "created_at": created_at.isoformat(),
                "expires_at": expires_at.isoformat(),
            },
        )

    async def release(self, sample_id: UUID) -> None:
        """Идемпотентно освободить слот, включая HTTP204 без JSON.

        Args:
            sample_id: ID терминального образца.
        """
        await self._request(HTTPMethod.DELETE, AdmissionPath.RELEASE.format(sample_id=sample_id))

    async def erase_owner(self, owner_id: UUID) -> None:
        """Использовать существующий trusted deletion endpoint primary Search.

        Args:
            owner_id: Авторизованно удалённый владелец.
        """
        await self._request(
            HTTPMethod.POST,
            AdmissionPath.DELETE_OWNER.format(owner_id=owner_id),
            {
                "event_id": str(uuid4()),
                "occurred_at": datetime.now(UTC).isoformat(),
            },
        )

    async def purge(self, now: datetime) -> None:
        """Оставить очистку primary maintenance, единственному владельцу ledger.

        Args:
            now: Время shadow maintenance; не отправляется как server TTL.
        """

    async def close(self) -> None:
        """Закрыть HTTP pool при завершении stateless процесса."""
        await self._client.aclose()

    async def _request(
        self, method: HTTPMethod, path: str, payload: dict[str, str] | None = None
    ) -> None:
        """Классифицировать отказ ресурса без раскрытия HTTP body либо URL.

        Args:
            method: Разрешённый метод.
            path: Фиксированный admission или deletion endpoint.
            payload: Технические идентичности и даты.
        """
        try:
            response = await self._client.request(method, path, json=payload)
        except (httpx.TransportError, httpx.InvalidURL, httpx.DecodingError) as error:
            raise SearchError(SearchErrorCode.UNAVAILABLE) from error
        failures: dict[int, SearchErrorCode] = {
            HTTPStatus.TOO_MANY_REQUESTS: SearchErrorCode.SAMPLE_LIMIT,
            HTTPStatus.FORBIDDEN: SearchErrorCode.FORBIDDEN,
            HTTPStatus.BAD_REQUEST: SearchErrorCode.INVALID_SAMPLE,
            HTTPStatus.UNPROCESSABLE_ENTITY: SearchErrorCode.INVALID_SAMPLE,
        }
        if response.status_code not in {HTTPStatus.OK, HTTPStatus.NO_CONTENT, HTTPStatus.CREATED}:
            raise SearchError(failures.get(response.status_code, SearchErrorCode.UNAVAILABLE))
