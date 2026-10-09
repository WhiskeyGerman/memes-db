"""Composition roots тестовых deployment поверх независимых Pg schemas."""

from dataclasses import dataclass, replace

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_search.application.dto.indexes import IndexTarget
from memes_search.application.dto.projections import EmbeddingVersion
from memes_search.application.dto.samples import InferenceState
from memes_search.application.dto.sessions import SearchSessionPolicy
from memes_search.application.ports.sample_admission import ISampleAdmission
from memes_search.application.use_cases.deletion import DeletePrivateData
from memes_search.application.use_cases.projections import ProjectionService
from memes_search.application.use_cases.samples import SampleService
from memes_search.application.use_cases.search import SearchService
from memes_search.domain.ranking import RankingPolicy
from memes_search.infrastructure.persistence.index import PostgreSQLSearchIndex
from memes_search.infrastructure.persistence.private_data import PostgreSQLPrivateData
from memes_search.infrastructure.persistence.samples import PostgreSQLSamples
from memes_search.infrastructure.persistence.sessions import PostgreSQLRequests, PostgreSQLSessions
from memes_search.presentation.container import Services
from memes_search.presentation.dependencies import get_services
from memes_search.presentation.links import SearchResourceLinks
from memes_search.presentation.main import create_app
from memes_search_tests.fakes import Inference
from memes_search_tests.harness import SearchHarness


@dataclass(frozen=True)
class IndexDeployment:
    database: async_sessionmaker[AsyncSession]
    version: EmbeddingVersion
    services: Services
    app: FastAPI


def build_deployment(
    database: async_sessionmaker[AsyncSession],
    harness: SearchHarness,
    version: EmbeddingVersion,
    target: IndexTarget,
    admission: ISampleAdmission,
) -> IndexDeployment:
    index = PostgreSQLSearchIndex(database, fuzzy_threshold=0.15)
    samples = PostgreSQLSamples(database)
    service = SearchService(
        index=index,
        sessions=PostgreSQLSessions(database),
        requests=PostgreSQLRequests(database),
        catalog=harness.catalog,
        inference=Inference(version=version, state=InferenceState.PENDING),
        samples=samples,
        clock=harness.clock,
        space_id=version.space,
        ranking=RankingPolicy(),
        session_policy=SearchSessionPolicy(),
    )
    services = replace(
        harness.services,
        index=index,
        sample_repository=samples,
        sample_admission=admission,
        search=service,
        projections=ProjectionService(
            index=index,
            samples=samples,
            admission=admission,
            clock=harness.clock,
            accepted_version=version,
        ),
        samples=SampleService(
            repository=samples,
            admission=admission,
            storage=harness.storage,
            inspector=harness.catalog,
            catalog=harness.catalog,
            clock=harness.clock,
            policy=SearchSessionPolicy(),
            index=index,
            embedding_version=version,
            index_target=target,
        ),
        delete_private_data=DeletePrivateData(
            repository=PostgreSQLPrivateData(database), admission=admission, clock=harness.clock
        ),
        links=SearchResourceLinks.for_target(target),
    )
    app = create_app()
    app.dependency_overrides[get_services] = lambda: services
    return IndexDeployment(database=database, version=version, services=services, app=app)
