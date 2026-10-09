from http import HTTPStatus

from fastapi.testclient import TestClient

from memes_identity.presentation.api.security import CookieName


def test_register_cookie_safety_and_refresh_rotation(
    identity_client: TestClient, account_password: str
) -> None:
    registered = identity_client.post(
        "/v1/auth/register", json={"login": "api_user", "password": account_password}
    )
    assert registered.status_code == HTTPStatus.CREATED
    assert "refresh_token" not in registered.json()
    refresh_cookie = next(
        value
        for value in registered.headers.get_list("set-cookie")
        if value.startswith(CookieName.REFRESH)
    )
    assert "HttpOnly" in refresh_cookie
    assert "SameSite=strict" in refresh_cookie
    assert "Path=/v1/auth" in refresh_cookie
    cookies = {cookie.name: cookie for cookie in identity_client.cookies.jar}
    assert cookies[CookieName.CSRF].path == "/"
    assert cookies[CookieName.REFRESH].path == "/v1/auth"
    csrf_cookie = next(
        value
        for value in registered.headers.get_list("set-cookie")
        if value.startswith(CookieName.CSRF)
    )
    assert "HttpOnly" not in csrf_cookie
    before = registered.json()["access_token"]
    csrf = identity_client.cookies.get(CookieName.CSRF)
    assert csrf is not None
    rotated = identity_client.post("/v1/auth/refresh", headers={"X-CSRF-Token": csrf})
    assert rotated.status_code == HTTPStatus.OK
    assert rotated.json()["access_token"] != before


def test_refresh_rejects_missing_or_forged_csrf(
    identity_client: TestClient, account_password: str
) -> None:
    identity_client.post(
        "/v1/auth/register", json={"login": "api_user", "password": account_password}
    )
    for headers in ({}, {"X-CSRF-Token": "forged"}):
        response = identity_client.post("/v1/auth/refresh", headers=headers)
        assert response.status_code == HTTPStatus.FORBIDDEN


def test_register_rejects_untrusted_origin(
    identity_client: TestClient, account_password: str
) -> None:
    response = identity_client.post(
        "/v1/auth/register",
        json={"login": "api_user", "password": account_password},
        headers={"Origin": "https://untrusted.invalid"},
    )
    assert response.status_code == HTTPStatus.FORBIDDEN


def test_internal_endpoints_require_shared_secret(
    identity_client: TestClient, account_password: str
) -> None:
    registered = identity_client.post(
        "/v1/auth/register", json={"login": "api_user", "password": account_password}
    )
    response = identity_client.post(
        "/internal/auth/introspect", json={"token": registered.json()["access_token"]}
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    response = identity_client.post(
        "/internal/auth/introspect",
        json={"token": registered.json()["access_token"]},
        headers={"X-Internal-Key": "k" * 32},
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["login"] == "api_user"
    assert "session_id" not in response.json()


def test_logout_revokes_bearer_session(identity_client: TestClient, account_password: str) -> None:
    registered = identity_client.post(
        "/v1/auth/register", json={"login": "api_user", "password": account_password}
    )
    bearer = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    assert identity_client.get("/v1/auth/me", headers=bearer).status_code == HTTPStatus.OK
    assert (
        identity_client.post("/v1/auth/logout", headers=bearer).status_code == HTTPStatus.NO_CONTENT
    )
    assert identity_client.get("/v1/auth/me", headers=bearer).status_code == HTTPStatus.UNAUTHORIZED


def test_validation_error_does_not_echo_secret_inputs(identity_client: TestClient) -> None:
    response = identity_client.post(
        "/v1/auth/register", json={"login": "api_user", "password": ["private-input"]}
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "private-input" not in response.text
    assert response.json() == {"code": "invalid_request"}
