from uuid import uuid4

import pytest
from pydantic import ValidationError

from memes_worker.infrastructure.config.settings import WorkerSettings


def test_worker_field_error_hides_device_secret(
    configuration_markers: tuple[str, str],
) -> None:
    marker = configuration_markers[0]
    with pytest.raises(ValidationError) as rejected:
        WorkerSettings(
            coordinator_url="https://processing.invalid",
            worker_id=uuid4(),
            token=marker,
            release_id="synthetic-config-test",
        )
    assert marker not in str(rejected.value)
