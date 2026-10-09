from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from enum import StrEnum
from os import environ
from typing import cast
from uuid import uuid4

import pytest
import pytest_asyncio
from _pytest.terminal import TerminalReporter
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_identity.application.dto.authentication import AccountDeleted, PasswordPolicy
from memes_identity.application.dto.outbox import DeletionDestination, DeletionRecipient
from memes_identity.application.use_cases.accounts import AccountUseCases
from memes_identity.application.use_cases.administration import (
    IdentityMaintenanceUseCases,
    OperatorUseCases,
)
from memes_identity.application.use_cases.authentication import AuthenticationUseCases
from memes_identity.application.use_cases.deletion_baseline import BaselineLimit
from memes_identity.domain.user import Login, User, UserId
from memes_identity.infrastructure.authentication.passwords import Argon2PasswordVerifier
from memes_identity.infrastructure.configuration import IdentitySettings
from memes_identity.infrastructure.persistence.maintenance import SqlIdentityMaintenance
from memes_identity.infrastructure.persistence.models import IdentityBase
from memes_identity.infrastructure.persistence.unit_of_work import SqlIdentityUnitOfWork
from memes_identity.presentation.dependencies import accounts, authentication
from memes_identity.presentation.main import create_app
from memes_identity_tests.configuration_fixtures import service_environment as service_environment
from memes_identity_tests.domain_fixtures import (
    now as now,
)
from memes_identity_tests.domain_fixtures import (
    user as user,
)
from memes_identity_tests.fakes import (
    FakePasswords,
    FakeUnitOfWork,
    FixedClock,
    MemoryDeletionJournal,
    MemoryState,
    PausingPasswords,
    RecordingDeletionPublisher,
)
from memes_platform.infrastructure.database import create_engine, session_factory
from memes_platform.infrastructure.deletion_journal import S3DeletionJournal


class TestAddress(StrEnum):
    ORIGIN = "http://testserver"
    DATABASE = "postgresql+asyncpg://identity:identity@127.0.0.1:5432/identity"


@pytest.fixture
def identity_settings() -> IdentitySettings:
    return IdentitySettings(
        database_url=TestAddress.DATABASE,
        internal_api_key="k" * 32,
        release_id="identity-test",
        catalog_url="http://catalog",
        search_url="http://search",
        journal_bucket="identity-journal",
        s3_access_key="test-key",
        s3_secret_key="test-secret",  # noqa: S106 - isolated test credentials
        s3_endpoint_url="http://127.0.0.1:58333",
        csrf_allowed_origins=[TestAddress.ORIGIN],
        cookie_secure=False,
        allow_insecure_http=True,
        password_memory_kib=8192,
        password_iterations=1,
        password_parallelism=1,
    )


@pytest.fixture
def identity_clock() -> FixedClock:
    return FixedClock(datetime(2026, 10, 2, tzinfo=UTC))


@pytest.fixture
def memory_state() -> MemoryState:
    return MemoryState()


@pytest.fixture
def fake_passwords() -> FakePasswords:
    return FakePasswords()


@pytest.fixture
def fake_uow(memory_state: MemoryState) -> FakeUnitOfWork:
    return FakeUnitOfWork(memory_state)


@pytest.fixture
def identity_authentication(
    fake_uow: FakeUnitOfWork, fake_passwords: FakePasswords, identity_clock: FixedClock
) -> AuthenticationUseCases:
    return AuthenticationUseCases(
        uow=fake_uow, passwords=fake_passwords, clock=identity_clock, policy=PasswordPolicy()
    )


@pytest.fixture
def identity_accounts(
    fake_uow: FakeUnitOfWork,
    fake_passwords: FakePasswords,
    identity_clock: FixedClock,
    fake_journal: MemoryDeletionJournal,
) -> AccountUseCases:
    return AccountUseCases(
        uow=fake_uow,
        passwords=fake_passwords,
        clock=identity_clock,
        policy=PasswordPolicy(),
        journal=fake_journal,
    )


@pytest.fixture
def account_password() -> str:
    return " " * 12


@pytest.fixture
def identity_client(
    identity_settings: IdentitySettings,
    identity_authentication: AuthenticationUseCases,
    identity_accounts: AccountUseCases,
) -> Iterator[TestClient]:
    application = create_app(identity_settings)
    application.dependency_overrides[authentication] = lambda: identity_authentication
    application.dependency_overrides[accounts] = lambda: identity_accounts
    with TestClient(application) as client:
        client.headers["Origin"] = TestAddress.ORIGIN
        yield client


@pytest_asyncio.fixture
async def sql_factory(
    identity_settings: IdentitySettings,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_url = environ.get("MEMES_TEST_IDENTITY_DATABASE_URL")
    if database_url is None:
        pytest.skip("MEMES_TEST_IDENTITY_DATABASE_URL не задан для PostgreSQL integration")
    database = make_url(database_url).database
    if database is None or not database.endswith("_test"):
        pytest.fail("Integration tests require a dedicated database ending in _test")
    engine = create_engine(database_url)
    async with engine.begin() as connection:
        await connection.run_sync(IdentityBase.metadata.drop_all)
        await connection.run_sync(IdentityBase.metadata.create_all)
    try:
        yield session_factory(engine)
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(IdentityBase.metadata.drop_all)
        await engine.dispose()


@pytest.fixture
def sql_authentication(
    sql_factory: async_sessionmaker[AsyncSession],
    identity_settings: IdentitySettings,
    identity_clock: FixedClock,
) -> Callable[[], AuthenticationUseCases]:
    passwords = Argon2PasswordVerifier(identity_settings)
    return lambda: AuthenticationUseCases(
        uow=SqlIdentityUnitOfWork(sql_factory, identity_settings),
        passwords=passwords,
        clock=identity_clock,
        policy=PasswordPolicy(),
    )


@pytest.fixture
def sql_accounts(
    sql_factory: async_sessionmaker[AsyncSession],
    identity_settings: IdentitySettings,
    identity_clock: FixedClock,
    fake_journal: MemoryDeletionJournal,
) -> AccountUseCases:
    return AccountUseCases(
        uow=SqlIdentityUnitOfWork(sql_factory, identity_settings),
        passwords=Argon2PasswordVerifier(identity_settings),
        clock=identity_clock,
        policy=PasswordPolicy(),
        journal=fake_journal,
    )


@pytest.fixture
def wrong_password() -> str:
    return "incorrect password"


@pytest.fixture
def new_password() -> str:
    return "new strong password"


@pytest.fixture
def fake_journal() -> MemoryDeletionJournal:
    return MemoryDeletionJournal()


@pytest.fixture
def s3_journal(identity_settings: IdentitySettings) -> Iterator[S3DeletionJournal]:
    endpoint = environ.get("MEMES_TEST_S3_ENDPOINT_URL")
    access_key = environ.get("MEMES_TEST_S3_ACCESS_KEY")
    secret_key = environ.get("MEMES_TEST_S3_SECRET_KEY")
    if not endpoint or not access_key or not secret_key:
        pytest.skip("S3 integration environment is not configured")
    bucket = f"identity-journal-test-{uuid4().hex}"
    settings = identity_settings.model_copy(
        update={
            "s3_endpoint_url": endpoint,
            "s3_access_key": access_key,
            "s3_secret_key": secret_key,
            "journal_bucket": bucket,
        }
    )
    journal = S3DeletionJournal(settings)
    journal.client.create_bucket(Bucket=bucket)
    try:
        yield journal
    finally:
        page = journal.client.list_objects_v2(Bucket=bucket)
        for item in page.get("Contents", []):
            if "Key" in item:
                journal.client.delete_object(Bucket=bucket, Key=item["Key"])
        journal.client.delete_bucket(Bucket=bucket)
        journal.close()


@pytest.fixture
def sql_identity_client(
    sql_factory: async_sessionmaker[AsyncSession],
    identity_settings: IdentitySettings,
) -> Iterator[TestClient]:
    database_url = environ["MEMES_TEST_IDENTITY_DATABASE_URL"]
    configured = identity_settings.model_copy(update={"database_url": database_url})
    with TestClient(create_app(configured)) as client:
        yield client


@pytest.fixture
def pausing_passwords(identity_settings: IdentitySettings) -> PausingPasswords:
    return PausingPasswords(Argon2PasswordVerifier(identity_settings))


@pytest.fixture
def sql_operators(
    sql_factory: async_sessionmaker[AsyncSession],
    identity_settings: IdentitySettings,
    identity_clock: FixedClock,
) -> OperatorUseCases:
    return OperatorUseCases(
        uow=SqlIdentityUnitOfWork(sql_factory, identity_settings),
        passwords=Argon2PasswordVerifier(identity_settings),
        clock=identity_clock,
        policy=PasswordPolicy(),
    )


@pytest.fixture
def sql_maintenance(
    sql_factory: async_sessionmaker[AsyncSession],
    identity_settings: IdentitySettings,
    identity_clock: FixedClock,
) -> IdentityMaintenanceUseCases:
    return IdentityMaintenanceUseCases(
        repository=SqlIdentityMaintenance(sql_factory),
        clock=identity_clock,
        login_window_seconds=identity_settings.login_window_seconds,
    )


@pytest.fixture
def deletion_event(identity_clock: FixedClock) -> AccountDeleted:
    return AccountDeleted(
        event_id=uuid4(),
        user_id=UserId(uuid4()),
        delete_media=True,
        occurred_at=identity_clock.now(),
    )


@pytest.fixture
def shadow_destination() -> DeletionDestination:
    return DeletionDestination(recipient=DeletionRecipient.SHADOW_SEARCH, base_url="http://shadow")


@pytest.fixture
def deletion_publisher() -> RecordingDeletionPublisher:
    return RecordingDeletionPublisher()


@pytest_asyncio.fixture
async def deleted_owners(
    sql_factory: async_sessionmaker[AsyncSession],
    identity_settings: IdentitySettings,
    identity_clock: FixedClock,
) -> tuple[UserId, ...]:
    owners: list[UserId] = []
    async with SqlIdentityUnitOfWork(sql_factory, identity_settings) as uow:
        for index in range(BaselineLimit.PAGE + 1):
            user = (
                User(
                    id=UserId(uuid4()),
                    login=Login(value=f"deleted_{index}"),
                    created_at=identity_clock.now(),
                )
                .begin_deletion(now=identity_clock.now())
                .finish_deletion(now=identity_clock.now())
            )
            await uow.users.save(user, None)
            owners.append(user.id)
        await uow.users.save(
            User(
                id=UserId(uuid4()),
                login=Login(value="active_owner"),
                created_at=identity_clock.now(),
            ),
            None,
        )
        await uow.commit()
    return tuple(owners)


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
