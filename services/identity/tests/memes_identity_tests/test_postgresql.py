import asyncio
from collections.abc import Callable
from enum import IntEnum

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_identity.application.dto.authentication import (
    AccountDeleted,
    IssuedTokens,
    PasswordPolicy,
)
from memes_identity.application.dto.outbox import DeletionDestination, DeletionRecipient
from memes_identity.application.errors import IdentityError, IdentityFailure
from memes_identity.application.use_cases.accounts import AccountUseCases
from memes_identity.application.use_cases.authentication import AuthenticationUseCases
from memes_identity.domain.user import UserState
from memes_identity.infrastructure.configuration import IdentitySettings
from memes_identity.infrastructure.persistence.models import (
    AccountOutboxRow,
    CredentialRow,
    SessionRow,
    TokenRow,
)
from memes_identity.infrastructure.persistence.unit_of_work import SqlIdentityUnitOfWork
from memes_identity_tests.fakes import FixedClock, PausingPasswords


@pytest.mark.asyncio
async def test_postgresql_stores_only_argon2_and_token_digests(
    sql_authentication: Callable[[], AuthenticationUseCases],
    sql_factory: async_sessionmaker[AsyncSession],
    account_password: str,
) -> None:
    tokens = await sql_authentication().register(
        login="persisted_user", password=account_password, peer_ip="127.0.0.1"
    )
    actor = await sql_authentication().authenticate(tokens.access_token)
    async with sql_factory() as session:
        verifier = await session.scalar(
            select(CredentialRow.verifier).where(CredentialRow.user_id == actor.user_id)
        )
        digests = tuple((await session.scalars(select(TokenRow.digest))).all())
    assert verifier is not None and verifier.startswith("$argon2id$")
    assert account_password not in verifier
    assert tokens.access_token not in digests
    assert tokens.refresh_token not in digests
    assert len(digests) == 2


@pytest.mark.asyncio
async def test_replayed_refresh_commits_family_revocation(
    sql_authentication: Callable[[], AuthenticationUseCases], account_password: str
) -> None:
    first = await sql_authentication().register(
        login="rotation_user", password=account_password, peer_ip="127.0.0.1"
    )
    rotated = await sql_authentication().refresh(first.refresh_token)
    assert rotated.session_id == first.session_id
    await sql_authentication().authenticate(rotated.access_token)
    with pytest.raises(IdentityError) as replay:
        await sql_authentication().refresh(first.refresh_token)
    assert replay.value.code is IdentityFailure.REFRESH_REPLAY
    with pytest.raises(IdentityError):
        await sql_authentication().authenticate(rotated.access_token)
    with pytest.raises(IdentityError):
        await sql_authentication().refresh(rotated.refresh_token)


@pytest.mark.asyncio
async def test_concurrent_rotation_has_one_winner_and_revokes_replayed_family(
    sql_authentication: Callable[[], AuthenticationUseCases], account_password: str
) -> None:
    first = await sql_authentication().register(
        login="concurrent_user", password=account_password, peer_ip="127.0.0.1"
    )
    results = await asyncio.gather(
        sql_authentication().refresh(first.refresh_token),
        sql_authentication().refresh(first.refresh_token),
        return_exceptions=True,
    )
    issued = [result for result in results if isinstance(result, IssuedTokens)]
    rejected = [result for result in results if isinstance(result, IdentityError)]
    assert len(issued) == len(rejected) == 1
    assert rejected[0].code is IdentityFailure.REFRESH_REPLAY
    with pytest.raises(IdentityError):
        await sql_authentication().authenticate(issued[0].access_token)


@pytest.mark.asyncio
async def test_deleted_account_erases_credential_and_session_rows_but_retains_outbox(
    sql_authentication: Callable[[], AuthenticationUseCases],
    sql_accounts: AccountUseCases,
    sql_factory: async_sessionmaker[AsyncSession],
    account_password: str,
) -> None:
    tokens = await sql_authentication().register(
        login="deletion_user", password=account_password, peer_ip="127.0.0.1"
    )
    actor = await sql_authentication().authenticate(tokens.access_token)
    await sql_accounts.delete(actor, password=account_password, delete_media=True)
    views = await sql_accounts.batch((actor.user_id,))
    assert views[0].state is UserState.DELETED
    assert views[0].login is None and views[0].delete_media
    async with sql_factory() as session:
        assert await session.scalar(select(CredentialRow)) is None
        assert await session.scalar(select(SessionRow)) is None
        assert await session.scalar(select(TokenRow)) is None
        event = await session.scalar(select(AccountOutboxRow))
        assert event is not None and event.user_id == actor.user_id and event.delete_media
    with pytest.raises(IdentityError):
        await sql_authentication().authenticate(tokens.access_token)


@pytest.mark.asyncio
async def test_postgresql_rate_limit_is_shared_across_distinct_uow(
    sql_factory: async_sessionmaker[AsyncSession],
    identity_settings: IdentitySettings,
    identity_clock: FixedClock,
) -> None:
    settings = identity_settings.model_copy(update={"login_account_attempts": 2})
    answers: list[bool] = []
    for _ in range(3):
        async with SqlIdentityUnitOfWork(sql_factory, settings) as uow:
            answers.append(
                await uow.rate_limiter.consume(
                    account="same_account", peer_ip="127.0.0.1", now=identity_clock.now()
                )
            )
            await uow.commit()
    assert answers == [True, True, False]


@pytest.mark.asyncio
async def test_outbox_skip_locked_does_not_deliver_same_recipient_concurrently(
    sql_factory: async_sessionmaker[AsyncSession],
    identity_settings: IdentitySettings,
    identity_clock: FixedClock,
    deletion_event: AccountDeleted,
) -> None:
    destination = DeletionDestination(
        recipient=DeletionRecipient.CATALOG, base_url="http://catalog"
    )
    async with SqlIdentityUnitOfWork(sql_factory, identity_settings) as seed:
        await seed.outbox.add(deletion_event, destination=destination)
        await seed.commit()
    async with SqlIdentityUnitOfWork(sql_factory, identity_settings) as first:
        delivery = await first.outbox.claim(identity_clock.now())
        assert delivery is not None
        async with SqlIdentityUnitOfWork(sql_factory, identity_settings) as second:
            assert await second.outbox.claim(identity_clock.now()) is None
        await first.commit()
    async with SqlIdentityUnitOfWork(sql_factory, identity_settings) as second:
        assert await second.outbox.claim(identity_clock.now()) is None
        await second.outbox.acknowledge(delivery, identity_clock.now())
        await second.commit()
    async with SqlIdentityUnitOfWork(sql_factory, identity_settings) as next_uow:
        assert await next_uow.outbox.claim(identity_clock.now()) is None


class RaceLimit(IntEnum):
    SECONDS = 10


@pytest.mark.asyncio
async def test_password_change_fences_login_verified_with_previous_password(
    sql_authentication: Callable[[], AuthenticationUseCases],
    sql_accounts: AccountUseCases,
    sql_factory: async_sessionmaker[AsyncSession],
    identity_settings: IdentitySettings,
    identity_clock: FixedClock,
    pausing_passwords: PausingPasswords,
    account_password: str,
    new_password: str,
) -> None:
    initial = await sql_authentication().register(
        login="password_race", password=account_password, peer_ip="127.0.0.1"
    )
    actor = await sql_authentication().authenticate(initial.access_token)
    login = AuthenticationUseCases(
        uow=SqlIdentityUnitOfWork(sql_factory, identity_settings),
        passwords=pausing_passwords,
        clock=identity_clock,
        policy=PasswordPolicy(),
    )
    pending = asyncio.create_task(
        login.login(login="password_race", password=account_password, peer_ip="127.0.0.1")
    )
    try:
        await asyncio.wait_for(pausing_passwords.checked.wait(), timeout=RaceLimit.SECONDS)
        await sql_accounts.change_password(
            actor, password=account_password, new_password=new_password
        )
    finally:
        pausing_passwords.resume.set()
    with pytest.raises(IdentityError) as rejected:
        await asyncio.wait_for(pending, timeout=RaceLimit.SECONDS)
    assert rejected.value.code is IdentityFailure.INVALID_CREDENTIALS
    accepted = await sql_authentication().login(
        login="password_race", password=new_password, peer_ip="127.0.0.1"
    )
    assert (await sql_authentication().authenticate(accepted.access_token)).user_id == actor.user_id
