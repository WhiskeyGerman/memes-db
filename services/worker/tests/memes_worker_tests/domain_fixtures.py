from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from memes_kernel.values import FileRef, Sha256


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def original_file() -> FileRef:
    content = b"original media fixture"
    return FileRef(
        id=uuid4(), sha256=Sha256(value=sha256(content).hexdigest()), size_bytes=len(content)
    )
