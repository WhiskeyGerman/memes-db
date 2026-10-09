from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from memes_identity.application.authentication import PasswordLimit, PasswordPolicy
from memes_identity.domain.user import AccountViolation, Login, User, UserState
from memes_kernel.errors import DomainError
from memes_kernel.values import Number


def test_login_is_case_insensitive_without_trimming() -> None:
    assert Login(value="User_NAME-01") == Login(value="user_name-01")
    with pytest.raises(DomainError) as error:
        Login(value=" user_name ")
    assert error.value.code is AccountViolation.INVALID_LOGIN


@pytest.mark.parametrize("value", ["ab", "русский", "name@example", "a" * 33, "\u212aelvin"])
def test_login_rejects_invalid_alphabet_or_length(value: str) -> None:
    with pytest.raises(DomainError):
        Login(value=value)


def test_password_is_not_trimmed() -> None:
    policy = PasswordPolicy()
    assert policy.accepts(" " * PasswordLimit.MINIMUM_LENGTH)
    assert not policy.accepts("x" * (PasswordLimit.MINIMUM_LENGTH - Number.ONE))
    assert not policy.accepts("x" * (PasswordLimit.MAXIMUM_LENGTH + Number.ONE))


def test_deletion_blocks_user_and_anonymizes_without_changing_identity(
    user: User, now: datetime
) -> None:
    deleting = user.begin_deletion(now=now)
    with pytest.raises(DomainError) as error:
        deleting.require_active()
    assert error.value.code is AccountViolation.INACTIVE
    deleted = deleting.finish_deletion(now=now + timedelta(seconds=Number.ONE))
    assert deleted.id == user.id
    assert deleted.login is None
    assert not deleted.roles
    assert deleted.state is UserState.DELETED
    assert deleted.begin_deletion(now=now) is deleted
    assert deleted.finish_deletion(now=now) is deleted


def test_deletion_cannot_finish_without_request(user: User, now: datetime) -> None:
    with pytest.raises(DomainError) as error:
        user.finish_deletion(now=now)
    assert error.value.code is AccountViolation.DELETION_NOT_STARTED


def test_entity_equality_uses_identity_and_value_objects_use_values(
    user: User, now: datetime
) -> None:
    assert user == user.begin_deletion(now=now)
    assert hash(user) == hash(replace(user, revision=user.revision.next()))
    assert Login(value="first") != Login(value="second")


def test_deleted_user_cannot_change_login(user: User, now: datetime) -> None:
    changed = user.change_login(Login(value="changed"))
    assert changed.id == user.id
    assert changed.login == Login(value="changed")
    assert changed.revision == user.revision.next()
    with pytest.raises(DomainError):
        user.begin_deletion(now=now).change_login(Login(value="changed"))
