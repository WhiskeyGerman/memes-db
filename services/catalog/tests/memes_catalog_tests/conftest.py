"""Фикстуры реальных PostgreSQL, S3, декодера и application Catalog."""

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from io import BytesIO
from os import environ
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from _pytest.terminal import TerminalReporter
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from memes_catalog.application.dto.catalog import Actor, ActorRole, MediaView, SearchIndexTarget
from memes_catalog.application.use_cases.catalog import CatalogUseCases
from memes_catalog.application.use_cases.variations import VariationUseCases
from memes_catalog.infrastructure.configuration import CatalogSettings
from memes_catalog.infrastructure.http.clients import IdentityClient, RestDelivery, RestOwner
from memes_catalog.infrastructure.media.inspector import MediaInspector
from memes_catalog.infrastructure.persistence.catalog import PostgresCatalogFactory
from memes_catalog.infrastructure.persistence.models import Base, CleanupRow, MediaRow, UploadRow
from memes_catalog.infrastructure.storage.s3 import S3ObjectStorage
from memes_catalog.presentation.dependencies import CatalogContainer, container
from memes_catalog.presentation.main import create_app
from memes_catalog_tests.configuration_fixtures import (
    configuration_markers as configuration_markers,
)
from memes_catalog_tests.configuration_fixtures import service_environment as service_environment
from memes_catalog_tests.domain_fixtures import (
    group as group,
)
from memes_catalog_tests.domain_fixtures import (
    image as image,
)
from memes_catalog_tests.domain_fixtures import (
    make_ballot as make_ballot,
)
from memes_catalog_tests.domain_fixtures import (
    media as media,
)
from memes_catalog_tests.domain_fixtures import (
    membership as membership,
)
from memes_catalog_tests.domain_fixtures import (
    now as now,
)
from memes_catalog_tests.domain_fixtures import (
    original_file as original_file,
)
from memes_catalog_tests.domain_fixtures import (
    other_group as other_group,
)
from memes_catalog_tests.domain_fixtures import (
    reach_threshold as reach_threshold,
)
from memes_catalog_tests.fakes import Clock, Delivery, Identity, Journal
from memes_platform.infrastructure.http import InternalHttpClient


@dataclass
class CatalogHarness:
    factory: PostgresCatalogFactory
    storage: S3ObjectStorage
    identity: Identity
    delivery: Delivery
    journal: Journal
    catalog: CatalogUseCases
    variations: VariationUseCases
    sessions: async_sessionmaker[AsyncSession]
    settings: CatalogSettings
    now: datetime

    async def publish(self, actor: Actor, body: bytes) -> MediaView:
        ticket = await self.catalog.begin(actor=actor, size_bytes=len(body), now=self.now)
        await asyncio.to_thread(
            self.storage.client.put_object,
            Bucket=self.storage.bucket,
            Key=ticket.upload.object_key,
            Body=body,
        )
        outcome = await self.catalog.complete(actor=actor, upload_id=ticket.upload.id, now=self.now)
        return await self.catalog.get(outcome.media_id)

    async def merge(self, first: MediaView, second: MediaView, actor: Actor) -> UUID:
        result = await self.variations.merge(
            actor=actor,
            first_id=first.group.id,
            second_id=second.group.id,
            first_revision=first.group.composition_revision.value,
            second_revision=second.group.composition_revision.value,
            now=self.now,
        )
        assert result.group_id is not None
        return result.group_id


@pytest.fixture
def png() -> Callable[[int], bytes]:
    def create(seed: int) -> bytes:
        output = BytesIO()
        Image.new("RGB", (32, 24), color=(seed, seed, seed)).save(output, format="PNG")
        return output.getvalue()

    return create


@pytest.fixture
def actor() -> Actor:
    return Actor(user_id=uuid4(), roles=frozenset({ActorRole.USER}))


@pytest.fixture
async def database() -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    url = os.environ.get("MEMES_CATALOG_TEST_DATABASE_URL")
    if url is None:
        pytest.skip("MEMES_CATALOG_TEST_DATABASE_URL requires actual PostgreSQL")
    schema = "catalog_test_" + uuid4().hex
    administrative = create_async_engine(url)
    async with administrative.begin() as connection:
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema + ",public"}}
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine, async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()
        async with administrative.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await administrative.dispose()


@pytest.fixture
def settings() -> CatalogSettings:
    endpoint = os.environ.get("MEMES_CATALOG_TEST_S3_ENDPOINT_URL")
    if endpoint is None:
        pytest.skip("MEMES_CATALOG_TEST_S3_ENDPOINT_URL requires actual S3")
    return CatalogSettings(
        database_url=os.environ["MEMES_CATALOG_TEST_DATABASE_URL"],
        internal_api_key=uuid4().hex,
        release_id="catalog-test",
        journal_bucket="catalog-test-journal",
        identity_url="http://identity.invalid",
        processing_url="http://processing.invalid",
        search_url="http://search.invalid",
        s3_endpoint_url=endpoint,
        s3_access_key=os.environ["MEMES_CATALOG_TEST_S3_ACCESS_KEY"],
        s3_secret_key=os.environ["MEMES_CATALOG_TEST_S3_SECRET_KEY"],
        s3_bucket=os.environ["MEMES_CATALOG_TEST_S3_BUCKET"],
        merge_threshold=1,
        detach_threshold=2,
    )


@pytest.fixture
async def harness(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    settings: CatalogSettings,
    now: datetime,
) -> AsyncIterator[CatalogHarness]:
    engine, sessions = database
    factory = PostgresCatalogFactory(engine, sessions)
    storage = S3ObjectStorage(settings)
    identity, delivery, journal = Identity(), Delivery(), Journal()
    policy = settings.media_policy()
    catalog = CatalogUseCases(
        factory,
        storage,
        MediaInspector(storage, policy),
        identity,
        delivery,
        policy=policy,
        clock=Clock(value=now),
        journal=journal,
        concurrent_limit=settings.concurrent_uploads,
        daily_limit=settings.daily_publications,
        storage_limit=settings.storage_capacity_bytes,
        free_percent=settings.free_storage_percent,
        queue_limit=settings.processing_queue_limit,
    )
    variations = VariationUseCases(
        factory,
        merge_threshold=settings.merge_threshold,
        detach_threshold=settings.detach_threshold,
        policy_revision=settings.voting_policy_revision,
    )
    try:
        yield CatalogHarness(
            factory=factory,
            storage=storage,
            identity=identity,
            delivery=delivery,
            journal=journal,
            catalog=catalog,
            variations=variations,
            sessions=sessions,
            settings=settings,
            now=now,
        )
    finally:
        async with sessions() as session:
            originals = list(await session.scalars(select(MediaRow.object_key)))
            staging = list(await session.scalars(select(UploadRow.object_key)))
            cleanup = list(await session.scalars(select(CleanupRow.object_key)))
        for key in set(originals + staging + cleanup):
            await storage.delete(key)
        storage.client.close()
        storage.signer.close()


@pytest.fixture
def app(harness: CatalogHarness) -> FastAPI:
    application = create_app()
    dependencies = CatalogContainer(
        settings=harness.settings,
        factory=harness.factory,
        storage=harness.storage,
        identity=cast(IdentityClient, harness.identity),
        delivery=cast(RestDelivery, harness.delivery),
        catalog=harness.catalog,
        variations=harness.variations,
    )
    application.dependency_overrides[container] = lambda: dependencies
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://catalog.invalid"
    ) as session:
        yield session


@pytest_asyncio.fixture
async def search_deliveries(
    settings: CatalogSettings,
) -> AsyncIterator[tuple[dict[SearchIndexTarget, RestDelivery], list[httpx.Request]]]:
    """Изолировать HTTP, сохранив реальные настройки каждого target."""
    captured: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(HTTPStatus.OK, json={"items": [], "accepted": True})

    instances: dict[SearchIndexTarget, RestDelivery] = {}
    for target in SearchIndexTarget:
        configured = settings.model_copy(
            update={
                "shadow_search_url": "http://shadow-search.invalid",
                "active_search_index": target,
            }
        )
        delivery = RestDelivery(configured)
        await delivery.close()
        delivery.clients = {
            owner: InternalHttpClient(
                base_url=url,
                internal_api_key=configured.internal_api_key,
                transport=httpx.MockTransport(capture),
            )
            for owner, url in (
                (RestOwner.SEARCH, configured.search_url),
                (RestOwner.SHADOW_SEARCH, "http://shadow-search.invalid"),
            )
        }
        instances[target] = delivery
    try:
        yield instances, captured
    finally:
        for instance in instances.values():
            await instance.close()


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
