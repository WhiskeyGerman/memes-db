"""Composition root общего ресурса admission без SQL в сценариях."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_search.application.dto.sessions import SearchSessionPolicy
from memes_search.application.ports.clock import IClock
from memes_search.application.ports.sample_admission import ISampleAdmission
from memes_search.application.use_cases.sample_admission import SharedSampleAdmission
from memes_search.infrastructure.configuration import Settings
from memes_search.infrastructure.persistence.sample_admission import PostgreSQLSampleAdmission
from memes_search.infrastructure.sample_admission import RestSampleAdmission


def create_admission(
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    clock: IClock,
    policy: SearchSessionPolicy,
) -> tuple[ISampleAdmission, RestSampleAdmission | None]:
    """Получить local primary ledger либо доверенный REST ресурс shadow."""
    if settings.sample_admission_url is not None:
        client = RestSampleAdmission(
            base_url=settings.sample_admission_url,
            internal_api_key=settings.internal_api_key,
            timeout=settings.http_timeout_seconds,
        )
        return client, client
    return (
        SharedSampleAdmission(
            repository=PostgreSQLSampleAdmission(factory), clock=clock, policy=policy
        ),
        None,
    )
