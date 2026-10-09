"""Независимая env-конфигурация разрешённых браузерных origins."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from memes_platform.infrastructure.configuration import RuntimeText


class CorsSettings(BaseSettings):
    """Origins задаются JSON-массивом, wildcard с credentials не применяется."""

    model_config = SettingsConfigDict(env_prefix=RuntimeText.PREFIX, extra="ignore")
    cors_allowed_origins: list[str] = Field(default_factory=list)
