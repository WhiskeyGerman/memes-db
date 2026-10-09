from datetime import UTC, datetime
from uuid import uuid4

import pytest

from memes_identity.domain.user import Login, User, UserId


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def user(now: datetime) -> User:
    return User(id=UserId(uuid4()), login=Login(value="fixture_user"), created_at=now)
