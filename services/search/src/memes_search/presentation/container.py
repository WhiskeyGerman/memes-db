"""Composition root Search; единственное место связывания внешних адаптеров."""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from memes_platform.infrastructure.database import create_engine, session_factory
from memes_platform.infrastructure.http import InternalHttpClient
from memes_search.application.dto.sessions import SearchSessionPolicy
from memes_search.application.ports.clock import IClock
from memes_search.application.ports.identity import IIdentityReader
from memes_search.application.ports.sample_admission import ISampleAdmission
from memes_search.application.ports.samples import ISampleRepository
from memes_search.application.ports.search_index import ISearchIndex
from memes_search.application.use_cases.deletion import DeletePrivateData
from memes_search.application.use_cases.projections import ProjectionService
from memes_search.application.use_cases.samples import SampleService
from memes_search.application.use_cases.search import SearchService
from memes_search.application.use_cases.suggestions import SuggestionService
from memes_search.domain.ranking import RankingPolicy
from memes_search.infrastructure.clients.catalog import CatalogRestReader
from memes_search.infrastructure.clients.identity import IdentityRestReader
from memes_search.infrastructure.clients.inference import ProcessingRestGateway
from memes_search.infrastructure.clock import SystemClock
from memes_search.infrastructure.configuration import SearchDefault, Settings
from memes_search.infrastructure.persistence.index import PostgreSQLSearchIndex
from memes_search.infrastructure.persistence.private_data import PostgreSQLPrivateData
from memes_search.infrastructure.persistence.samples import PostgreSQLSamples
from memes_search.infrastructure.persistence.sessions import PostgreSQLRequests, PostgreSQLSessions
from memes_search.infrastructure.sample_admission import RestSampleAdmission
from memes_search.infrastructure.storage import S3PrivateStorage
from memes_search.presentation.admission import create_admission
from memes_search.presentation.links import SearchResourceLinks


@dataclass(frozen=True, slots=True, kw_only=True)
class Services:
    """Прикладные сценарии и порты, доступные контроллерам."""

    links: SearchResourceLinks
    projections: ProjectionService
    delete_private_data: DeletePrivateData
    search: SearchService
    suggestions: SuggestionService
    samples: SampleService
    index: ISearchIndex
    sample_repository: ISampleRepository
    sample_admission: ISampleAdmission
    identity: IIdentityReader
    clock: IClock


@dataclass(frozen=True, slots=True, kw_only=True)
class Runtime:
    """Ресурсы, принадлежащие одному stateless web-процессу."""

    services: Services
    engine: AsyncEngine
    clients: tuple[InternalHttpClient | RestSampleAdmission, ...]
    storage: S3PrivateStorage

    async def ready(self) -> bool:
        """Проверить связь с БД и наличие применённых таблиц, без ML."""
        try:
            async with self.engine.connect() as connection:
                return (
                    await connection.scalar(
                        text(
                            "SELECT to_regclass('search_media') IS NOT NULL "
                            "AND to_regclass('search_deleted_owners') IS NOT NULL "
                            "AND to_regclass('search_text_fragments') IS NOT NULL "
                            "AND to_regclass('search_sample_admissions') IS NOT NULL"
                        )
                    )
                    is True
                )
        except SQLAlchemyError:
            return False

    async def close(self) -> None:
        """Корректно закрыть HTTP, S3 и PostgreSQL при graceful shutdown."""
        for client in self.clients:
            await client.close()
        self.storage.close()
        await self.engine.dispose()


def create_runtime(settings: Settings) -> Runtime:
    """Собрать application через infrastructure из окружения релиза."""
    engine = create_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_pool_overflow,
    )
    factory = session_factory(engine)
    catalog_client = InternalHttpClient(
        base_url=settings.catalog_url,
        internal_api_key=settings.internal_api_key,
        timeout=settings.http_timeout_seconds,
    )
    processing_client = InternalHttpClient(
        base_url=settings.processing_url,
        internal_api_key=settings.internal_api_key,
        timeout=settings.http_timeout_seconds,
    )
    identity_client = InternalHttpClient(
        base_url=settings.identity_url,
        internal_api_key=settings.internal_api_key,
        timeout=settings.http_timeout_seconds,
    )
    catalog = CatalogRestReader(catalog_client)
    inference = ProcessingRestGateway(
        processing_client,
        wait_seconds=settings.query_wait_milliseconds / SearchDefault.MILLISECONDS_PER_SECOND,
        poll_seconds=settings.query_poll_milliseconds / SearchDefault.MILLISECONDS_PER_SECOND,
        dimensions=settings.embedding_dimensions,
        space_id=settings.embedding_space_id,
        version=settings.accepted_embedding_version,
    )
    clock = SystemClock()
    policy = SearchSessionPolicy(
        lifetime=timedelta(seconds=settings.sample_session_seconds),
        maximum_sample_bytes=settings.maximum_sample_bytes,
        active_samples_per_user=settings.active_samples_per_user,
        maximum_query_characters=settings.maximum_query_characters,
        download_seconds=settings.sample_download_seconds,
    )
    admission, admission_client = create_admission(settings, factory, clock, policy)
    index = PostgreSQLSearchIndex(
        factory,
        fuzzy_threshold=settings.fuzzy_threshold,
        ann_fragment_multiplier=settings.ann_fragment_multiplier,
    )
    sessions = PostgreSQLSessions(factory)
    requests = PostgreSQLRequests(factory)
    samples = PostgreSQLSamples(factory)
    storage = S3PrivateStorage(settings)
    services = Services(
        links=SearchResourceLinks.for_target(settings.index_target),
        projections=ProjectionService(
            index=index,
            samples=samples,
            admission=admission,
            clock=clock,
            accepted_version=settings.accepted_embedding_version,
        ),
        delete_private_data=DeletePrivateData(
            repository=PostgreSQLPrivateData(factory), admission=admission, clock=clock
        ),
        search=SearchService(
            index=index,
            sessions=sessions,
            requests=requests,
            catalog=catalog,
            inference=inference,
            samples=samples,
            clock=clock,
            space_id=settings.embedding_space_id,
            ranking=RankingPolicy(),
            session_policy=policy,
        ),
        suggestions=SuggestionService(
            index=index,
            catalog=catalog,
            threshold=settings.variation_similarity_threshold,
            rules=settings.variation_similarity_rules,
        ),
        samples=SampleService(
            repository=samples,
            admission=admission,
            storage=storage,
            inspector=catalog,
            catalog=catalog,
            clock=clock,
            policy=policy,
            index=index,
            embedding_version=settings.accepted_embedding_version,
            index_target=settings.index_target,
        ),
        index=index,
        sample_repository=samples,
        sample_admission=admission,
        identity=IdentityRestReader(identity_client),
        clock=clock,
    )
    return Runtime(
        services=services,
        engine=engine,
        clients=(catalog_client, processing_client, identity_client)
        + ((admission_client,) if admission_client is not None else ()),
        storage=storage,
    )
