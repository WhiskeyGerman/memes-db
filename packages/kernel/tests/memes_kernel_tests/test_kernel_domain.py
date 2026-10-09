from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID

import pytest

from memes_kernel.errors import DomainError
from memes_kernel.values import (
    CommonViolation,
    FileRef,
    Number,
    Revision,
    Sha256,
    require_aware,
    require_identifier,
    require_ordered,
)


def test_timezone_and_temporal_order_are_required(now: datetime) -> None:
    with pytest.raises(DomainError) as naive:
        require_aware(now.replace(tzinfo=None))
    assert naive.value.code is CommonViolation.NAIVE_DATETIME
    with pytest.raises(DomainError) as reversed_time:
        require_ordered(now - timedelta(seconds=Number.ONE), now)
    assert reversed_time.value.code is CommonViolation.TIME_REVERSED


def test_nil_identifier_is_invalid() -> None:
    with pytest.raises(DomainError) as error:
        require_identifier(UUID(int=Number.ZERO))
    assert error.value.code is CommonViolation.INVALID_IDENTIFIER


def test_revision_is_positive() -> None:
    with pytest.raises(DomainError):
        Revision(value=Number.ZERO)
    assert Revision().next().value == Revision().value + Number.ONE


def test_digest_is_validated_and_file_is_nonempty(original_file: FileRef) -> None:
    assert Sha256(value=original_file.sha256.value.upper()) == original_file.sha256
    with pytest.raises(DomainError):
        Sha256(value="invalid-sha")
    with pytest.raises(DomainError):
        replace(original_file, size_bytes=Number.ZERO)
