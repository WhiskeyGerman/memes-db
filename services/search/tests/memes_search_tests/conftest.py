"""Повторно используемые состояния сценариев и фикстура HTTP-клиента."""

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta
from os import environ
from typing import cast
from uuid import uuid4

import httpx
import pytest
from _pytest.terminal import TerminalReporter
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_search.application.dto.execution import SearchRequest
from memes_search.application.dto.indexes import IndexTarget
from memes_search.application.dto.projections import (
    EmbeddingVersion,
    FeatureKind,
    FeatureProjection,
    ProjectionState,
)
from memes_search.application.dto.queries import SearchMediaKind, SearchQuery
from memes_search.application.dto.samples import PrivateSample, SampleState
from memes_search.application.dto.sessions import SearchSessionPolicy
from memes_search.application.use_cases.deletion import DeletePrivateData
from memes_search.application.use_cases.projections import ProjectionService
from memes_search.application.use_cases.sample_admission import SharedSampleAdmission
from memes_search.application.use_cases.samples import SampleService
from memes_search.application.use_cases.search import SearchService
from memes_search.application.use_cases.suggestions import SuggestionService
from memes_search.domain.ranking import RankingPolicy
from memes_search.infrastructure.persistence.sample_admission import PostgreSQLSampleAdmission
from memes_search.presentation.container import Services
from memes_search.presentation.dependencies import get_services
from memes_search.presentation.links import SearchResourceLinks
from memes_search.presentation.main import create_app
from memes_search_tests.configuration_fixtures import configuration_markers as configuration_markers
from memes_search_tests.configuration_fixtures import service_environment as service_environment
from memes_search_tests.databases import isolated_database
from memes_search_tests.deployments import IndexDeployment, build_deployment
from memes_search_tests.domain_fixtures import (
    now as now,
)
from memes_search_tests.fakes import (
    Admission,
    Catalog,
    Clock,
    Identity,
    Index,
    Inference,
    MultipartSpools,
    PrivateData,
    ReceiveSpy,
    Requests,
    Samples,
    Sessions,
    Storage,
)
from memes_search_tests.harness import SearchHarness


@pytest.fixture
def harness(now: datetime, vector_version: EmbeddingVersion) -> SearchHarness:
    clock = Clock(value=now)
    catalog, index, sessions, requests = Catalog(), Index(), Sessions(), Requests()
    samples, storage, inference, identity = (
        Samples(),
        Storage(),
        Inference(version=vector_version),
        Identity(),
    )
    policy = SearchSessionPolicy()
    admission = Admission(clock=clock, limit=policy.active_samples_per_user)
    search = SearchService(
        index=index,
        sessions=sessions,
        requests=requests,
        catalog=catalog,
        inference=inference,
        samples=samples,
        clock=clock,
        space_id="fixture-space",
        ranking=RankingPolicy(),
        session_policy=policy,
    )
    sample_service = SampleService(
        repository=samples,
        admission=admission,
        storage=storage,
        inspector=catalog,
        catalog=catalog,
        clock=clock,
        policy=policy,
        index=index,
        embedding_version=vector_version,
    )
    services = Services(
        links=SearchResourceLinks.for_target(IndexTarget.PRIMARY),
        projections=ProjectionService(
            index=index,
            samples=samples,
            admission=admission,
            clock=clock,
            accepted_version=vector_version,
        ),
        delete_private_data=DeletePrivateData(
            repository=PrivateData(sessions=sessions, requests=requests, samples=samples),
            admission=admission,
            clock=clock,
        ),
        suggestions=SuggestionService(index=index, catalog=catalog, threshold=0.80),
        search=search,
        samples=sample_service,
        index=index,
        sample_repository=samples,
        sample_admission=admission,
        identity=identity,
        clock=clock,
    )
    return SearchHarness(
        services=services,
        clock=clock,
        catalog=catalog,
        index=index,
        sessions=sessions,
        requests=requests,
        samples=samples,
        storage=storage,
        inference=inference,
        identity=identity,
        version=vector_version,
        admission=admission,
    )


@pytest.fixture
def app(harness: SearchHarness) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_services] = lambda: harness.services
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://search.invalid"
    ) as session:
        yield session


@pytest.fixture
async def database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    url = os.environ.get("MEMES_SEARCH_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("MEMES_SEARCH_TEST_DATABASE_URL requires real pgvector PostgreSQL")
    async with isolated_database(url) as factory:
        yield factory


@pytest.fixture
async def dual_databases() -> AsyncIterator[
    tuple[async_sessionmaker[AsyncSession], async_sessionmaker[AsyncSession]]
]:
    url = os.environ.get("MEMES_SEARCH_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("MEMES_SEARCH_TEST_DATABASE_URL requires real pgvector PostgreSQL")
    async with isolated_database(url) as old, isolated_database(url) as new:
        yield old, new


@pytest.fixture
def projection(now: datetime) -> FeatureProjection:
    return FeatureProjection(
        event_id=uuid4(),
        media_id=uuid4(),
        group_id=uuid4(),
        group_revision=1,
        revision=1,
        published_at=now,
        media_kind=SearchMediaKind.IMAGE,
        kind=FeatureKind.OCR,
        cycle_id=uuid4(),
        generation=1,
        state=ProjectionState.AVAILABLE,
        version_key="fixture-ocr-v1",
        text="кот сидит на столе funny cat",
    )


@pytest.fixture
def private_sample(now: datetime) -> PrivateSample:
    return PrivateSample(
        id=uuid4(),
        owner_id=uuid4(),
        object_key="samples/private-fixture/file",
        created_at=now,
        expires_at=now + timedelta(minutes=10),
        state=SampleState.QUEUED,
    )


@pytest.fixture
def private_request(private_sample: PrivateSample, now: datetime) -> SearchRequest:
    return SearchRequest(
        id=uuid4(),
        query=SearchQuery(text="private fixture", owner_id=private_sample.owner_id),
        created_at=now,
        expires_at=private_sample.expires_at,
    )


@pytest.fixture
def vector_version() -> EmbeddingVersion:
    return EmbeddingVersion(
        model="fixture-model",
        weights="fixture-weights",
        task_adapter="fixture-adapter",
        preprocessing="fixture-v1",
        space="fixture-space",
        dimensions=2,
    )


@pytest.fixture
def receive_spy(app: FastAPI) -> ReceiveSpy:
    return ReceiveSpy(app)


@pytest.fixture
async def spy_client(receive_spy: ReceiveSpy) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=receive_spy), base_url="http://search.invalid"
    ) as client:
        yield client


@pytest.fixture
def multipart_spools(monkeypatch: pytest.MonkeyPatch) -> Iterator[MultipartSpools]:
    with ExitStack() as stack:
        spools = MultipartSpools(stack=stack)
        monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", spools.create)
        yield spools


@pytest.fixture
def deployments(
    dual_databases: tuple[async_sessionmaker[AsyncSession], async_sessionmaker[AsyncSession]],
    harness: SearchHarness,
    vector_version: EmbeddingVersion,
) -> tuple[IndexDeployment, IndexDeployment]:
    old, new = dual_databases
    admission = SharedSampleAdmission(
        repository=PostgreSQLSampleAdmission(old), clock=harness.clock, policy=SearchSessionPolicy()
    )
    return (
        build_deployment(old, harness, vector_version, IndexTarget.PRIMARY, admission),
        build_deployment(
            new,
            harness,
            replace(vector_version, weights="fixture-weights-v2"),
            IndexTarget.SHADOW,
            admission,
        ),
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Не допустить зелёный CI при пропущенных integration проверках."""
    if environ.get("MEMES_REQUIRE_INTEGRATION") != "1":
        return
    reporter = cast(
        TerminalReporter | None, session.config.pluginmanager.get_plugin("terminalreporter")
    )
    if reporter is not None and reporter.stats.get("skipped"):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter.write_line("Integration CI must not skip tests", red=True)
