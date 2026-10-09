"""Конфигурация Search из окружения конкретного релиза."""

from enum import IntEnum, StrEnum
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from memes_platform.infrastructure.configuration import BaseServiceSettings
from memes_search.application.dto.indexes import IndexTarget
from memes_search.application.dto.projections import EmbeddingVersion
from memes_search.application.dto.queries import SearchLimit
from memes_search.application.dto.suggestions import SimilarityThreshold


class SearchDefault(IntEnum):
    """Начальные эксплуатационные параметры поиска."""

    QUERY_WAIT_MILLISECONDS = 1_500
    QUERY_POLL_MILLISECONDS = 100
    DELIVERY_BATCH = 20
    DELIVERY_LEASE_SECONDS = 120
    DELIVERY_RETRY_SECONDS = 5
    DELIVERY_MAX_RETRY_SECONDS = 300
    FUZZY_THRESHOLD_PERCENT = 15
    ANN_FRAGMENT_MULTIPLIER = 4
    COSINE_THRESHOLD_PERCENT = 80
    MILLISECONDS_PER_SECOND = 1_000
    S3_ATTEMPTS = 3
    S3_CONNECT_TIMEOUT_SECONDS = 5
    S3_READ_TIMEOUT_SECONDS = 30


class SearchText(StrEnum):
    """Нейтральные постоянные имена продукта."""

    SPACE = "jina-v5-omni-nano"
    TITLE = "Memes search"
    FIXED_DIMENSIONS = (
        "embedding dimensions require a separate schema artifact; current index is 768"
    )
    IMPORT_PATH = "memes_search.presentation.main:app"


class Settings(BaseServiceSettings):
    """Окружение web и maintenance процессов одного Search-релиза."""

    index_target: IndexTarget = IndexTarget.PRIMARY
    sample_admission_url: str | None = None
    accepted_embedding_version: EmbeddingVersion
    catalog_url: str = Field(min_length=1)
    identity_url: str = Field(min_length=1)
    processing_url: str = Field(min_length=1)
    s3_bucket: str = Field(min_length=1)
    s3_access_key: str = Field(min_length=1, repr=False)
    s3_secret_key: str = Field(min_length=1, repr=False)
    variation_similarity_rules: tuple[SimilarityThreshold, ...] = ()
    s3_public_endpoint_url: str | None = None
    s3_connect_timeout_seconds: int = Field(default=SearchDefault.S3_CONNECT_TIMEOUT_SECONDS, ge=1)
    s3_read_timeout_seconds: int = Field(default=SearchDefault.S3_READ_TIMEOUT_SECONDS, ge=1)
    s3_retry_attempts: int = Field(default=SearchDefault.S3_ATTEMPTS, ge=1)
    embedding_space_id: str = Field(default=SearchText.SPACE, min_length=1)
    embedding_dimensions: int = Field(
        default=SearchLimit.EMBEDDING_INDEX_DIMENSIONS,
        ge=1,
        le=SearchLimit.MAXIMUM_VECTOR_DIMENSIONS,
    )
    query_wait_milliseconds: int = Field(default=SearchDefault.QUERY_WAIT_MILLISECONDS, ge=0)
    query_poll_milliseconds: int = Field(default=SearchDefault.QUERY_POLL_MILLISECONDS, ge=1)
    maximum_sample_bytes: int = Field(
        default=SearchLimit.MAXIMUM_SAMPLE_BYTES, ge=1, le=SearchLimit.MAXIMUM_SAMPLE_BYTES
    )
    active_samples_per_user: int = Field(default=SearchLimit.ACTIVE_SAMPLES_PER_USER, ge=1)
    maximum_query_characters: int = Field(
        default=SearchLimit.QUERY_CHARACTERS, ge=1, le=SearchLimit.QUERY_CHARACTERS
    )
    sample_download_seconds: int = Field(
        default=SearchLimit.DOWNLOAD_SECONDS, ge=1, le=SearchLimit.DOWNLOAD_SECONDS
    )
    sample_session_seconds: int = Field(default=SearchLimit.SESSION_SECONDS, ge=1)
    ann_fragment_multiplier: int = Field(default=SearchDefault.ANN_FRAGMENT_MULTIPLIER, ge=1)
    delivery_lease_seconds: int = Field(default=SearchDefault.DELIVERY_LEASE_SECONDS, ge=1)
    delivery_retry_seconds: int = Field(default=SearchDefault.DELIVERY_RETRY_SECONDS, ge=1)
    delivery_max_retry_seconds: int = Field(default=SearchDefault.DELIVERY_MAX_RETRY_SECONDS, ge=1)
    delivery_batch: int = Field(default=SearchDefault.DELIVERY_BATCH, ge=1)
    maintenance_interval_seconds: int = Field(default=SearchLimit.CLEANUP_SECONDS, ge=1)
    variation_similarity_threshold: float = Field(
        default=SearchDefault.COSINE_THRESHOLD_PERCENT / 100, gt=0, le=1
    )
    fuzzy_threshold: float = Field(default=SearchDefault.FUZZY_THRESHOLD_PERCENT / 100, gt=0, le=1)

    @field_validator("embedding_dimensions")
    @classmethod
    def require_physical_dimensions(cls, value: int) -> int:
        """Отклонить deployment, несовместимый с закреплённым физическим индексом."""
        if value != SearchLimit.EMBEDDING_INDEX_DIMENSIONS:
            raise ValueError(SearchText.FIXED_DIMENSIONS)
        return value

    @field_validator("sample_admission_url")
    @classmethod
    def validate_admission_url(cls, value: str | None) -> str | None:
        """Потребовать явно выбранный доверенный REST origin без credentials."""
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("sample admission requires a trusted HTTP origin without credentials")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_resources(self) -> "Settings":
        """Потребовать адреса всех ресурсов, которыми владеет Search."""
        if not all((self.catalog_url, self.identity_url, self.processing_url, self.s3_bucket)):
            raise ValueError("Search requires catalog, identity, processing URLs and S3 bucket")
        if (
            self.accepted_embedding_version.space != self.embedding_space_id
            or self.accepted_embedding_version.dimensions != self.embedding_dimensions
        ):
            raise ValueError("accepted_embedding_passport_must_match_space_and_dimensions")
        if self.index_target is IndexTarget.SHADOW and self.sample_admission_url is None:
            raise ValueError("shadow index requires MEMES_SAMPLE_ADMISSION_URL")
        if self.index_target is IndexTarget.PRIMARY and self.sample_admission_url is not None:
            raise ValueError("primary index must own the sample admission ledger")
        return self
