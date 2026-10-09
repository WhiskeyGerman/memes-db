from datetime import UTC, datetime

import pytest


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 1, tzinfo=UTC)
