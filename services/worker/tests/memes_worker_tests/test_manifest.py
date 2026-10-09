from pathlib import Path

import pytest

from memes_worker.application.dto.runtime import WorkerExecutionError
from memes_worker.infrastructure.ml.manifest import verify_manifest, write_manifest


@pytest.fixture
def weights(tmp_path: Path) -> Path:
    (tmp_path / "weights.bin").write_bytes(b"immutable weights")
    write_manifest(tmp_path, model="test-model", revision="release-v1")
    return tmp_path


def test_revision_cannot_be_claimed_for_other_weights(weights: Path) -> None:
    verify_manifest(weights, model="test-model", revision="release-v1")
    with pytest.raises(WorkerExecutionError):
        verify_manifest(weights, model="test-model", revision="release-v2")
    (weights / "weights.bin").write_bytes(b"modified")
    with pytest.raises(WorkerExecutionError):
        verify_manifest(weights, model="test-model", revision="release-v1")
