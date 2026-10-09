"""Параметры Identity из окружения текущего релиза."""

from enum import IntEnum, StrEnum

from pydantic import Field

from memes_platform.infrastructure.configuration import BaseServiceSettings


class IdentityDefault(IntEnum):
    """Исходные эксплуатационные параметры, переопределяемые окружением."""

    ACCESS_TTL_SECONDS = 900
    REFRESH_TTL_SECONDS = 2592000
    LOGIN_WINDOW_SECONDS = 60
    LOGIN_ACCOUNT_ATTEMPTS = 10
    LOGIN_IP_ATTEMPTS = 60
    PASSWORD_MEMORY_KIB = 65536
    PASSWORD_ITERATIONS = 3
    PASSWORD_PARALLELISM = 4
    OUTBOX_POLL_SECONDS = 1
    OUTBOX_RETRY_SECONDS = 5
    OUTBOX_RETRY_MAX_SECONDS = 300
    OUTBOX_LEASE_SECONDS = 120
    MAX_SESSIONS = 20


class IdentityConfiguration(StrEnum):
    """Машинные ошибки конфигурации."""

    CATALOG_REQUIRED = "MEMES_CATALOG_URL is required"
    SEARCH_REQUIRED = "MEMES_SEARCH_URL is required"
    INVALID_OUTBOX_LEASE = "outbox_lease_must_exceed_http_timeout"
    ORIGINS_REQUIRED = "MEMES_CSRF_ALLOWED_ORIGINS is required"
    INSECURE_COOKIE = "MEMES_COOKIE_SECURE may be false only with MEMES_ALLOW_INSECURE_HTTP"


class IdentitySettings(BaseServiceSettings):
    """Настройки сроков, ограничений и браузерных cookie."""

    access_ttl_seconds: int = Field(default=IdentityDefault.ACCESS_TTL_SECONDS, ge=1)
    refresh_ttl_seconds: int = Field(default=IdentityDefault.REFRESH_TTL_SECONDS, ge=1)
    login_window_seconds: int = Field(default=IdentityDefault.LOGIN_WINDOW_SECONDS, ge=1)
    login_account_attempts: int = Field(default=IdentityDefault.LOGIN_ACCOUNT_ATTEMPTS, ge=1)
    login_ip_attempts: int = Field(default=IdentityDefault.LOGIN_IP_ATTEMPTS, ge=1)
    password_memory_kib: int = Field(default=IdentityDefault.PASSWORD_MEMORY_KIB, ge=8192)
    password_iterations: int = Field(default=IdentityDefault.PASSWORD_ITERATIONS, ge=1)
    password_parallelism: int = Field(default=IdentityDefault.PASSWORD_PARALLELISM, ge=1)
    max_sessions: int = Field(default=IdentityDefault.MAX_SESSIONS, ge=1)
    outbox_poll_seconds: float = Field(default=IdentityDefault.OUTBOX_POLL_SECONDS, gt=0)
    outbox_retry_seconds: int = Field(default=IdentityDefault.OUTBOX_RETRY_SECONDS, ge=1)
    outbox_retry_max_seconds: int = Field(default=IdentityDefault.OUTBOX_RETRY_MAX_SECONDS, ge=1)
    outbox_lease_seconds: int = Field(default=IdentityDefault.OUTBOX_LEASE_SECONDS, ge=1)
    shadow_search_url: str | None = None
    cookie_secure: bool = True
    allow_insecure_http: bool = False
    csrf_allowed_origins: list[str] = Field(default_factory=list)

    def require_http_configuration(self) -> None:
        """Проверить обязательные параметры браузерного API.

        Raises:
            ValueError: Не задан источник браузера либо отключена защита cookie.
        """
        if not self.csrf_allowed_origins:
            raise ValueError(IdentityConfiguration.ORIGINS_REQUIRED)
        if not self.cookie_secure and not self.allow_insecure_http:
            raise ValueError(IdentityConfiguration.INSECURE_COOKIE)

    def require_outbox_configuration(self) -> None:
        """Проверить подключаемый REST-ресурс фонового процесса.

        Raises:
            ValueError: Получатель уведомлений не задан.
        """
        if not self.catalog_url:
            raise ValueError(IdentityConfiguration.CATALOG_REQUIRED)
        if not self.search_url:
            raise ValueError(IdentityConfiguration.SEARCH_REQUIRED)
        if self.outbox_lease_seconds <= self.http_timeout_seconds:
            raise ValueError(IdentityConfiguration.INVALID_OUTBOX_LEASE)
