"""Административные сценарии через Application и реальные PostgreSQL порты."""

from collections.abc import Callable
from datetime import timedelta
from enum import IntEnum
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from memes_identity.application.errors import IdentityError, IdentityFailure
from memes_identity.application.use_cases.accounts import AccountUseCases
from memes_identity.application.use_cases.administration import (
    IdentityMaintenanceUseCases,
    OperatorUseCases,
)
from memes_identity.application.use_cases.authentication import AuthenticationUseCases
from memes_identity.domain.user import UserRole, UserState
from memes_identity.infrastructure.persistence.models import (
    AccountOutboxRow,
    RateBucketRow,
    SessionRow,
    TokenRow,
    UserRow,
)
from memes_identity_tests.fakes import FixedClock


class RetentionFixture(IntEnum):
    OLD_DAYS = 31
    OLD_RATE_SECONDS = 121


@pytest.mark.integration
async def test_bootstrap_promotes_existing_account_and_revokes_its_sessions(
    sql_operators: OperatorUseCases,
    sql_authentication: Callable[[], AuthenticationUseCases],
    account_password: str,
) -> None:
    created = await sql_operators.bootstrap(login="operator_admin", password=account_password)
    assert UserRole.OPERATOR in created.roles
    tokens = await sql_authentication().login(
        login="operator_admin", password=account_password, peer_ip="127.0.0.1"
    )
    assert (await sql_authentication().authenticate(tokens.access_token)).user_id == created.user_id
    promoted = await sql_operators.bootstrap(login="operator_admin", password=None)
    assert promoted.user_id == created.user_id
    with pytest.raises(IdentityError) as revoked:
        await sql_authentication().authenticate(tokens.access_token)
    assert revoked.value.code is IdentityFailure.UNAUTHORIZED
    assert (
        await sql_authentication().login(
            login="operator_admin", password=account_password, peer_ip="127.0.0.1"
        )
    ).access_token


@pytest.mark.integration
async def test_bootstrap_without_valid_password_cannot_create_an_account(
    sql_operators: OperatorUseCases,
    sql_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(IdentityError) as invalid:
        await sql_operators.bootstrap(login="invalid_admin", password=None)
    assert invalid.value.code is IdentityFailure.INVALID_PASSWORD_LENGTH
    async with sql_factory() as session:
        assert await session.scalar(select(UserRow)) is None


@pytest.mark.integration
async def test_cleanup_preserves_live_sessions_pending_delivery_and_deleted_account(
    sql_maintenance: IdentityMaintenanceUseCases,
    sql_authentication: Callable[[], AuthenticationUseCases],
    sql_accounts: AccountUseCases,
    sql_factory: async_sessionmaker[AsyncSession],
    identity_clock: FixedClock,
    account_password: str,
) -> None:
    authentication = sql_authentication()
    expired = await authentication.register(
        login="expired_admin", password=account_password, peer_ip="127.0.0.1"
    )
    live = await sql_authentication().register(
        login="live_admin", password=account_password, peer_ip="127.0.0.1"
    )
    deleting = await sql_authentication().register(
        login="deleted_admin", password=account_password, peer_ip="127.0.0.1"
    )
    actor = await sql_authentication().authenticate(deleting.access_token)
    await sql_accounts.delete(actor, password=account_password, delete_media=True)
    old = identity_clock.now() - timedelta(days=RetentionFixture.OLD_DAYS)
    completed_id, recent_id = uuid4(), uuid4()
    async with sql_factory.begin() as session:
        await session.execute(
            update(SessionRow)
            .where(SessionRow.id == expired.session_id)
            .values(expires_at=identity_clock.now())
        )
        session.add(
            RateBucketRow(
                key=uuid4().hex,
                attempts=1,
                window_started_at=identity_clock.now()
                - timedelta(seconds=RetentionFixture.OLD_RATE_SECONDS),
            )
        )
        for identifier, delivered_at in ((completed_id, old), (recent_id, identity_clock.now())):
            session.add(
                AccountOutboxRow(
                    event_id=identifier,
                    user_id=actor.user_id,
                    delete_media=True,
                    occurred_at=old,
                    next_attempt_at=old,
                    attempts=0,
                    delivered_at=delivered_at,
                )
            )
    await sql_maintenance.cleanup()
    async with sql_factory() as session:
        assert await session.get(SessionRow, expired.session_id) is None
        assert not tuple(
            await session.scalars(select(TokenRow).where(TokenRow.session_id == expired.session_id))
        )
        assert await session.get(SessionRow, live.session_id) is not None
        assert await session.get(AccountOutboxRow, completed_id) is None
        assert await session.get(AccountOutboxRow, recent_id) is not None
        assert (
            await session.scalar(
                select(AccountOutboxRow).where(AccountOutboxRow.delivered_at.is_(None))
            )
            is not None
        )
        deleted = await session.get(UserRow, actor.user_id)
        assert deleted is not None and deleted.state == UserState.DELETED
        assert deleted.login is None and deleted.delete_media
