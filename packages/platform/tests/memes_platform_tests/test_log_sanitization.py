import pytest

from memes_platform.infrastructure.log_sanitization import LogSanitizer, environment_secrets


@pytest.mark.parametrize(
    "url",
    [
        "https://storage.example:9443/memes/cat%20face.png?versionId=v1&partNumber=2#preview",
        "https://[2001:db8::1]:9428/select/logsql/query?limit=20",
        "HTTP://EXAMPLE.ORG/a%2Fb?q=a+b&q=a%20b&empty=#part",
        "s3://memes-media/2026/cat.png",
        "postgresql+asyncpg://db.internal:5432/memes_catalog?sslmode=require",
        "https://storage.example/bucket/it's.png?part=2",
        "/v1/media/4f263f65-bbbe-4b27-b39b-dfb2803994d9?limit=10",
        "https://storage.example/file?token_count=3&api_key_rotation=weekly&key=cat.png",
    ],
)
def test_safe_resource_urls_are_preserved_exactly(url: str) -> None:
    assert LogSanitizer().redact(url) == url


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "https://user:p%40ss@storage.example:9443/bucket/cat.png?part=2#preview",
            "https://[REDACTED]@storage.example:9443/bucket/cat.png?part=2#preview",
        ),
        (
            "https://user:p'a:ss@storage.example/bucket/cat.png?part=2",
            "https://[REDACTED]@storage.example/bucket/cat.png?part=2",
        ),
        (
            "https://storage.example/file?versionId=v1&token=first&part=2&token=second#preview",
            "https://storage.example/file?versionId=v1&token=[REDACTED]&part=2&token=[REDACTED]#preview",
        ),
        (
            "https://storage.example/file?X-Amz-Signature=unknown&X-Amz-Credential=ACCESS%2Fscope&X-Amz-Date=20261016T130000Z",
            "https://storage.example/file?X-Amz-Signature=[REDACTED]&X-Amz-Credential=[REDACTED]&X-Amz-Date=20261016T130000Z",
        ),
        (
            "https://storage.example/file?to%6ben=unknown&name=cat%20face.png&name=dog+face.png",
            "https://storage.example/file?to%6ben=[REDACTED]&name=cat%20face.png&name=dog+face.png",
        ),
        (
            "https://storage.example/file#access_token=unknown&state=preview",
            "https://storage.example/file#access_token=[REDACTED]&state=preview",
        ),
        (
            "http://user:password@[broken-host/path?token=unknown&part=2",
            "http://[REDACTED]@[broken-host/path?token=[REDACTED]&part=2",
        ),
        (
            'Failed GET "https://storage.example/file?token=unknown&part=2" with HTTP 403',
            'Failed GET "https://storage.example/file?token=[REDACTED]&part=2" with HTTP 403',
        ),
        (
            "Failed GET 'https://storage.example/file?token=unknown&part=2' with HTTP 403",
            "Failed GET 'https://storage.example/file?token=[REDACTED]&part=2' with HTTP 403",
        ),
        (
            'password="https://credential.example/private" resource=https://storage.example/file',
            'password="[REDACTED]" resource=https://storage.example/file',
        ),
        (
            '{"Authorization": "Bearer unknown", "url": "https://storage.example/file"}',
            '{"Authorization": "[REDACTED]", "url": "https://storage.example/file"}',
        ),
        ("Authorization: Basic dXNlcjpwYXNz", "Authorization: [REDACTED]"),
    ],
)
def test_only_credentials_are_removed_and_resource_identity_survives(
    source: str, expected: str
) -> None:
    sanitizer = LogSanitizer()
    assert sanitizer.redact(source) == expected
    assert sanitizer.redact(expected) == expected


def test_environment_dsn_does_not_turn_the_entire_resource_address_into_a_secret() -> None:
    dsn = "postgresql+asyncpg://reader:p%40ssword@db.internal:5432/memes_catalog"
    credentials = environment_secrets(
        {
            "MEMES_DATABASE_URL": dsn,
            "MEMES_INTERNAL_API_KEY": "opaque-internal-key",
            "MEMES_PASSWORD_POLICY": "development",
        }
    )
    assert dsn not in credentials
    assert "p@ssword" in credentials
    assert "development" not in credentials
    sanitized = LogSanitizer(credentials).redact(f"Cannot connect to {dsn}")
    assert sanitized == (
        "Cannot connect to postgresql+asyncpg://[REDACTED]@db.internal:5432/memes_catalog"
    )


def test_known_secret_is_removed_when_percent_encoded_in_a_nonstandard_parameter() -> None:
    sanitizer = LogSanitizer(("secret/with+specials",))
    assert (
        sanitizer.redact("https://storage.example/cat.png?custom=secret%2fwith%2bspecials&part=2")
        == "https://storage.example/cat.png?custom=[REDACTED]&part=2"
    )


def test_multiple_urls_and_useful_failure_reason_survive_in_one_message() -> None:
    source = (
        "Copy https://source.example/bucket/cat.png to "
        "https://destination.example/bucket/cat.png?token=unknown&part=2 failed: HTTP 403"
    )
    assert LogSanitizer().redact(source) == source.replace("token=unknown", "token=[REDACTED]")
