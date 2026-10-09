from uuid import uuid4

import pytest

from memes_identity.application.dto.authentication import AccountDeleted, AccountEventKind
from memes_identity.application.errors import IdentityError
from memes_identity.application.use_cases.accounts import AccountUseCases
from memes_identity.application.use_cases.authentication import AuthenticationUseCases
from memes_identity.domain.user import UserId, UserState
from memes_identity_tests.fakes import (
    FakeUnitOfWork,
    FixedClock,
    MemoryDeletionJournal,
    MemoryState,
)
from memes_identity_tests.test_application import register_actor
from memes_platform.application.errors import ApplicationError


async def test_unavailable_journal_keeps_account_and_secrets(
    identity_authentication: AuthenticationUseCases,
    identity_accounts: AccountUseCases,
    fake_journal: MemoryDeletionJournal,
    memory_state: MemoryState,
    account_password: str,
) -> None:
    actor, tokens = await register_actor(identity_authentication, account_password)
    fake_journal.available = False
    with pytest.raises(ApplicationError):
        await identity_accounts.delete(actor, password=account_password, delete_media=True)
    assert memory_state.users[actor.user_id].state is UserState.ACTIVE
    assert actor.user_id in memory_state.credentials
    assert not memory_state.outbox
    assert await identity_authentication.authenticate(tokens.access_token)


async def test_journal_replay_finishes_intent_after_failed_commit(
    identity_authentication: AuthenticationUseCases,
    identity_accounts: AccountUseCases,
    fake_journal: MemoryDeletionJournal,
    fake_uow: FakeUnitOfWork,
    memory_state: MemoryState,
    account_password: str,
) -> None:
    actor, _ = await register_actor(identity_authentication, account_password)
    fake_uow.fail_next_commit = True
    with pytest.raises(IdentityError):
        await identity_accounts.delete(actor, password=account_password, delete_media=True)
    assert memory_state.users[actor.user_id].state is UserState.ACTIVE
    assert len(fake_journal.events) == 1
    await identity_accounts.replay_deletion_journal()
    await identity_accounts.replay_deletion_journal()
    assert memory_state.users[actor.user_id].state is UserState.DELETED
    assert len(memory_state.outbox) == 1
    assert actor.user_id not in memory_state.credentials


async def test_restore_creates_missing_tombstone_and_choice_never_weakens(
    identity_accounts: AccountUseCases,
    identity_clock: FixedClock,
    memory_state: MemoryState,
    fake_journal: MemoryDeletionJournal,
) -> None:
    user_id = UserId(uuid4())
    initial = AccountDeleted(
        event_id=uuid4(),
        user_id=user_id,
        delete_media=True,
        occurred_at=identity_clock.now(),
    )
    await fake_journal.record(
        {
            "kind": AccountEventKind.ACCOUNT,
            "event_id": str(initial.event_id),
            "user_id": str(user_id),
            "delete_media": initial.delete_media,
            "occurred_at": initial.occurred_at.isoformat(),
        }
    )
    await identity_accounts.replay_deletion_journal()
    await identity_accounts.apply_recorded_deletion(
        AccountDeleted(
            event_id=uuid4(),
            user_id=user_id,
            delete_media=False,
            occurred_at=identity_clock.now(),
        )
    )
    deleted = memory_state.users[user_id]
    assert deleted.state is UserState.DELETED
    assert deleted.login is None
    assert deleted.delete_media
    assert len(memory_state.outbox) == 2
