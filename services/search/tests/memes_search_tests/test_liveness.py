from http import HTTPStatus

from fastapi.testclient import TestClient

from memes_search.presentation.main import create_app


def test_service_liveness(service_environment: None) -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health/live")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"status": "alive"}
