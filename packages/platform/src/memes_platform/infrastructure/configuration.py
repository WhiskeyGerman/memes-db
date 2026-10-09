"""Конфигурация развёртывания исключительно из окружения."""

from enum import IntEnum, StrEnum

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeDefault(IntEnum):
    """Нейтральные значения параметров процесса."""

    HTTP_PORT = 8080
    HTTP_TIMEOUT_SECONDS = 15
    DATABASE_POOL_SIZE = 10
    DATABASE_POOL_OVERFLOW = 10
    SHUTDOWN_SECONDS = 30


class RuntimeText(StrEnum):
    """Имена окружения и нейтральные настройки процесса."""

    PREFIX = "MEMES_"
    HOST = "0.0.0.0"  # noqa: S104 - port binding управляется окружением.
    LOG_LEVEL = "INFO"
    POSTGRES_SCHEME = "postgresql+asyncpg://"


class BaseServiceSettings(BaseSettings):
    """Общие параметры подключаемых ресурсов.

    Адреса и секреты задаются окружением конкретного релиза. Неиспользуемый
    ресурс можно оставить пустым; сервис проверяет свои обязательные поля.
    """

    model_config = SettingsConfigDict(
        env_prefix=RuntimeText.PREFIX, extra="ignore", hide_input_in_errors=True
    )

    database_url: str = Field(repr=False)
    internal_api_key: str = Field(min_length=32, repr=False)
    release_id: str = Field(min_length=1)
    host: str = RuntimeText.HOST
    port: int = Field(default=RuntimeDefault.HTTP_PORT, ge=1, le=65535)
    log_level: str = RuntimeText.LOG_LEVEL
    identity_url: str = ""
    catalog_url: str = ""
    processing_url: str = ""
    search_url: str = ""
    s3_endpoint_url: str | None = None
    s3_access_key: str = Field(default="", repr=False)
    s3_secret_key: str = Field(default="", repr=False)
    s3_bucket: str = ""
    journal_bucket: str = ""
    s3_region: str = "us-east-1"
    http_timeout_seconds: float = Field(default=RuntimeDefault.HTTP_TIMEOUT_SECONDS, gt=0)
    database_pool_size: int = Field(default=RuntimeDefault.DATABASE_POOL_SIZE, ge=1)
    database_pool_overflow: int = Field(default=RuntimeDefault.DATABASE_POOL_OVERFLOW, ge=0)
    shutdown_seconds: int = Field(default=RuntimeDefault.SHUTDOWN_SECONDS, ge=1)

    @field_validator("database_url")
    @classmethod
    def validate_postgres(cls, value: str) -> str:
        """Потребовать явно выбранный драйвер PostgreSQL.

        Args:
            value: DSN из окружения.
        """
        if not value.startswith(RuntimeText.POSTGRES_SCHEME):
            raise ValueError("MEMES_DATABASE_URL must use postgresql+asyncpg")
        return value
