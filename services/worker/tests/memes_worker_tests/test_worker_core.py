from dataclasses import replace
from uuid import uuid4

import pytest

from memes_kernel.values import FileRef, Number
from memes_worker.application.execution import ExecutionKey, Operation, WorkerCapabilities


@pytest.fixture
def execution_key(original_file: FileRef) -> ExecutionKey:
    return ExecutionKey(
        source_id=uuid4(),
        source_hash=original_file.sha256,
        cycle_id=uuid4(),
        operation=Operation.OCR,
        fragment_id=uuid4(),
        model_revision="model-v1",
        preprocessing_revision="sampling-v1",
    )


def test_same_bytes_from_other_source_do_not_share_cache_key(execution_key: ExecutionKey) -> None:
    another = replace(execution_key, source_id=uuid4())
    assert another.source_hash == execution_key.source_hash
    assert another != execution_key


def test_new_cycle_or_model_revision_changes_cache_identity(execution_key: ExecutionKey) -> None:
    assert replace(execution_key, cycle_id=uuid4()) != execution_key
    assert replace(execution_key, model_revision="model-v2") != execution_key


def test_worker_cannot_advertise_zero_capacity() -> None:
    with pytest.raises(ValueError):
        WorkerCapabilities(
            operations=frozenset({Operation.OCR}), maximum_parallel_tasks=Number.ZERO
        )
