from collections.abc import Callable
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_identity.application.dto.authentication import PasswordPolicy
from memes_identity.application.use_cases.accounts import AccountUseCases
from memes_identity.application.use_cases.authentication import AuthenticationUseCases
from memes_identity.domain.user import UserState
from memes_identity.infrastructure.authentication.passwords import Argon2PasswordVerifier
from memes_identity.infrastructure.configuration import IdentitySettings
from memes_identity.infrastructure.persistence.models import AccountOutboxRow, UserRow
from memes_identity.infrastructure.persistence.unit_of_work import SqlIdentityUnitOfWork
from memes_identity_tests.fakes import FixedClock
from memes_platform.application.dto.json import JsonObject
from memes_platform.infrastructure.deletion_journal import S3DeletionJournal


@pytest.mark.integration
async def test_external_s3_journal_replays_after_sql_state_is_lost(
    sql_factory: async_sessionmaker[AsyncSession],
    sql_authentication: Callable[[], AuthenticationUseCases],
    identity_settings: IdentitySettings,
    identity_clock: FixedClock,
    s3_journal: S3DeletionJournal,
    account_password: str,
) -> None:
    accounts = AccountUseCases(
        uow=SqlIdentityUnitOfWork(sql_factory, identity_settings),
        passwords=Argon2PasswordVerifier(identity_settings),
        clock=identity_clock,
        policy=PasswordPolicy(),
        journal=s3_journal,
    )
    tokens = await sql_authentication().register(
        login="journal_user",
        password=account_password,
        peer_ip="127.0.0.1",
    )
    actor = await sql_authentication().authenticate(tokens.access_token)
    await accounts.delete(actor, password=account_password, delete_media=True)
    records = [event async for event in s3_journal.records()]
    assert len(records) == 1
    assert records[0]["user_id"] == str(actor.user_id)
    # Имитировать recovery point, в котором SQL deletion/tombstone ещё отсутствуют.
    async with sql_factory() as session:
        await session.execute(delete(AccountOutboxRow))
        await session.execute(delete(UserRow).where(UserRow.id == actor.user_id))
        await session.commit()
    await accounts.replay_deletion_journal()
    await accounts.replay_deletion_journal()
    view = (await accounts.batch((actor.user_id,)))[0]
    assert view.state is UserState.DELETED
    assert view.login is None and view.delete_media
    async with sql_factory() as session:
        outbox = list((await session.scalars(select(AccountOutboxRow))).all())
        assert len(outbox) == 1
        assert outbox[0].user_id == actor.user_id


@pytest.mark.integration
async def test_external_journal_same_event_is_immutable(s3_journal: S3DeletionJournal) -> None:
    event_id = uuid4()
    event: JsonObject = {"event_id": str(event_id), "kind": "account", "delete_media": True}
    await s3_journal.record(event)
    await s3_journal.record({**event, "delete_media": False})
    records = [entry async for entry in s3_journal.records()]
    assert len(records) == 1
    assert records[0]["delete_media"] is True
