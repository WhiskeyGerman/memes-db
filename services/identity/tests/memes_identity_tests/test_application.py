import pytest

from memes_identity.application.dto.authentication import AuthenticatedActor, IssuedTokens
from memes_identity.application.errors import IdentityError, IdentityFailure
from memes_identity.application.use_cases.accounts import AccountUseCases
from memes_identity.application.use_cases.authentication import AuthenticationUseCases
from memes_identity.domain.user import UserRole, UserState
from memes_identity_tests.fakes import FakePasswords, FakeUnitOfWork, MemoryState


async def register_actor(
    authentication: AuthenticationUseCases, password: str
) -> tuple[AuthenticatedActor, IssuedTokens]:
    tokens = await authentication.register(
        login="Test_User", password=password, peer_ip="127.0.0.1"
    )
    return await authentication.authenticate(tokens.access_token), tokens


@pytest.mark.asyncio
async def test_registration_normalizes_login_and_preserves_password(
    identity_authentication: AuthenticationUseCases,
    memory_state: MemoryState,
    account_password: str,
) -> None:
    actor, tokens = await register_actor(identity_authentication, account_password)
    assert actor.login == "test_user"
    assert memory_state.credentials[actor.user_id] == account_password
    assert tokens.access_token not in memory_state.credentials.values()
    with pytest.raises(IdentityError) as error:
        await identity_authentication.register(
            login="TEST_USER", password=account_password, peer_ip="127.0.0.1"
        )
    assert error.value.code is IdentityFailure.LOGIN_OCCUPIED


@pytest.mark.asyncio
async def test_unknown_and_bad_password_have_same_failure_and_expensive_verification(
    identity_authentication: AuthenticationUseCases,
    fake_passwords: FakePasswords,
    fake_uow: FakeUnitOfWork,
    account_password: str,
    wrong_password: str,
) -> None:
    await register_actor(identity_authentication, account_password)
    errors: list[IdentityFailure] = []
    for login in ("missing_user", "test_user", "bad login"):
        with pytest.raises(IdentityError) as error:
            await identity_authentication.login(
                login=login, password=wrong_password, peer_ip="127.0.0.1"
            )
        errors.append(error.value.code)
    assert errors == [IdentityFailure.INVALID_CREDENTIALS] * 3
    assert len(fake_passwords.verified) == 3
    assert fake_uow.commits == 4


@pytest.mark.asyncio
async def test_rate_limit_failure_is_committed(
    identity_authentication: AuthenticationUseCases,
    memory_state: MemoryState,
    wrong_password: str,
) -> None:
    memory_state.allowed = False
    with pytest.raises(IdentityError) as error:
        await identity_authentication.login(
            login="missing_user", password=wrong_password, peer_ip="127.0.0.1"
        )
    assert error.value.code is IdentityFailure.RATE_LIMITED
    assert memory_state.attempts == 1


@pytest.mark.asyncio
async def test_password_change_preserves_current_session_and_revokes_others(
    identity_authentication: AuthenticationUseCases,
    identity_accounts: AccountUseCases,
    memory_state: MemoryState,
    account_password: str,
    new_password: str,
) -> None:
    actor, current = await register_actor(identity_authentication, account_password)
    other = await identity_authentication.login(
        login=actor.login, password=account_password, peer_ip="127.0.0.1"
    )
    await identity_accounts.change_password(
        actor, password=account_password, new_password=new_password
    )
    assert current.session_id not in memory_state.revoked
    assert other.session_id in memory_state.revoked
    assert (
        await identity_authentication.authenticate(current.access_token)
    ).user_id == actor.user_id


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_media", [False, True])
async def test_delete_account_atomically_erases_secrets_and_creates_outbox(
    identity_authentication: AuthenticationUseCases,
    identity_accounts: AccountUseCases,
    memory_state: MemoryState,
    account_password: str,
    delete_media: bool,
) -> None:
    actor, tokens = await register_actor(identity_authentication, account_password)
    await identity_accounts.delete(actor, password=account_password, delete_media=delete_media)
    deleted = memory_state.users[actor.user_id]
    assert deleted.state is UserState.DELETED
    assert deleted.login is None
    assert not deleted.roles
    assert deleted.delete_media is delete_media
    assert actor.user_id not in memory_state.credentials
    assert not memory_state.sessions
    assert memory_state.outbox[0].delete_media is delete_media
    views = await identity_accounts.batch((actor.user_id,))
    assert views[0].login is None
    assert views[0].delete_media is delete_media
    with pytest.raises(IdentityError):
        await identity_authentication.authenticate(tokens.access_token)
    replacement = await identity_authentication.register(
        login=actor.login, password=account_password, peer_ip="127.0.0.1"
    )
    assert (
        await identity_authentication.authenticate(replacement.access_token)
    ).user_id != actor.user_id


@pytest.mark.asyncio
async def test_wrong_password_does_not_delete_or_enqueue(
    identity_authentication: AuthenticationUseCases,
    identity_accounts: AccountUseCases,
    memory_state: MemoryState,
    account_password: str,
    wrong_password: str,
) -> None:
    actor, _ = await register_actor(identity_authentication, account_password)
    with pytest.raises(IdentityError):
        await identity_accounts.delete(actor, password=wrong_password, delete_media=True)
    assert memory_state.users[actor.user_id].state is UserState.ACTIVE
    assert actor.user_id in memory_state.credentials
    assert not memory_state.outbox


@pytest.mark.asyncio
async def test_regular_user_cannot_change_access(
    identity_authentication: AuthenticationUseCases,
    identity_accounts: AccountUseCases,
    account_password: str,
) -> None:
    actor, _ = await register_actor(identity_authentication, account_password)
    with pytest.raises(IdentityError) as error:
        await identity_accounts.change_access(
            actor, user_id=actor.user_id, blocked=True, roles=frozenset({UserRole.USER})
        )
    assert error.value.code is IdentityFailure.FORBIDDEN
