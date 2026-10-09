from http import HTTPStatus

from fastapi.testclient import TestClient


def test_http_readiness_and_metrics_use_real_postgresql(sql_identity_client: TestClient) -> None:
    response = sql_identity_client.get("/health/ready")
    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "ready"
    assert response.headers.get("X-Request-ID")
    metrics = sql_identity_client.get("/metrics")
    assert metrics.status_code == HTTPStatus.OK
    assert "memes_http_requests_total" in metrics.text
    assert "memes_outbox_pending" in metrics.text
