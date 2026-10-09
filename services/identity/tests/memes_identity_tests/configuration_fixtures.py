from uuid import uuid4

import pytest


@pytest.fixture
def service_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Задать безопасное окружение только для проверки живости сервисов."""
    values = {
        "MEMES_DATABASE_URL": "postgresql+asyncpg://fixture:fixture@127.0.0.1:1/fixture",
        "MEMES_INTERNAL_API_KEY": uuid4().hex + uuid4().hex,
        "MEMES_RELEASE_ID": "liveness-test",
        "MEMES_ACCEPTED_EMBEDDING_VERSION": (
            '{"model":"fixture-model","weights":"fixture-weights",'
            '"task_adapter":"retrieval-v1","preprocessing":"ffmpeg-sampling-v1",'
            '"space":"jina-v5-omni-nano","dimensions":768}'
        ),
        "MEMES_IDENTITY_URL": "http://testserver",
        "MEMES_CATALOG_URL": "http://testserver",
        "MEMES_PROCESSING_URL": "http://testserver",
        "MEMES_SEARCH_URL": "http://testserver",
        "MEMES_S3_ENDPOINT_URL": "http://testserver",
        "MEMES_S3_ACCESS_KEY": "liveness-fixture",
        "MEMES_S3_SECRET_KEY": uuid4().hex,
        "MEMES_S3_BUCKET": "liveness-fixture",
        "MEMES_JOURNAL_BUCKET": "liveness-fixture",
        "MEMES_CSRF_ALLOWED_ORIGINS": '["http://testserver"]',
        "MEMES_COOKIE_SECURE": "false",
        "MEMES_ALLOW_INSECURE_HTTP": "true",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
