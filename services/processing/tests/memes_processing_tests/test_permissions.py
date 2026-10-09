from http import HTTPStatus
from uuid import uuid4

import httpx
import pytest

from memes_processing.application.dto.coordinator import CoordinatorCode, CoordinatorError
from memes_processing.application.use_cases.permissions import AuthorizeOperator
from memes_processing.infrastructure.http.identity import RESTIdentityGateway
from memes_processing_tests.fixtures import CoordinatorFixture


@pytest.mark.asyncio
async def test_identity_transport_and_role_policy_are_separate(
    coordinator: CoordinatorFixture,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                HTTPStatus.OK,
                json={
                    "user_id": str(uuid4()),
                    "login": "reader",
                    "roles": ["user"],
                },
            )
        )
    ) as client:
        with pytest.raises(CoordinatorError) as error:
            await AuthorizeOperator(
                identity=RESTIdentityGateway(client, coordinator.settings),
                token=uuid4().hex,
            ).execute()
        assert error.value.code is CoordinatorCode.FORBIDDEN


@pytest.mark.asyncio
async def test_identity_unavailability_is_not_permission_denial(
    coordinator: CoordinatorFixture,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(HTTPStatus.SERVICE_UNAVAILABLE),
        )
    ) as client:
        with pytest.raises(CoordinatorError) as error:
            await AuthorizeOperator(
                identity=RESTIdentityGateway(client, coordinator.settings),
                token=uuid4().hex,
            ).execute()
        assert error.value.code is CoordinatorCode.UNAVAILABLE
