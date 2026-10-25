from http import HTTPStatus

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from memes_identity_tests.conftest import TestAddress as IdentityAddress


def test_http_readiness_and_metrics_use_real_postgresql(sql_identity_client: TestClient) -> None:
    response = sql_identity_client.get("/health/ready")
    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "ready"
    assert response.headers.get("X-Request-ID")
    metrics = sql_identity_client.get("/metrics")
    assert metrics.status_code == HTTPStatus.OK
    assert "memes_http_requests_total" in metrics.text
    assert "memes_outbox_pending" in metrics.text


def test_active_users_metric_tracks_successful_registration(
    sql_identity_client: TestClient, account_password: str
) -> None:
    def active_users() -> float:
        response = sql_identity_client.get("/metrics")
        assert response.status_code == HTTPStatus.OK
        samples = {
            sample.name: sample.value
            for family in text_string_to_metric_families(response.text)
            for sample in family.samples
        }
        return float(samples["memes_users_active"])

    assert active_users() == 0
    sql_identity_client.headers["Origin"] = IdentityAddress.ORIGIN
    response = sql_identity_client.post(
        "/v1/auth/register", json={"login": "metrics-user", "password": account_password}
    )
    assert response.status_code == HTTPStatus.CREATED
    assert active_users() == 1
    response = sql_identity_client.post(
        "/v1/auth/register", json={"login": "metrics-user", "password": account_password}
    )
    assert response.status_code == HTTPStatus.CONFLICT
    assert active_users() == 1
