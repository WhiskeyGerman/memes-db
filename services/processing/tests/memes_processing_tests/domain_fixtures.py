from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from memes_kernel.values import FileRef, Number, Sha256
from memes_processing.application.tasks import ProcessingTask, TaskInput
from memes_processing.domain.features import (
    CycleId,
    ExtractionId,
    FeatureExtraction,
    FeatureKind,
    FeatureVersion,
)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def original_file() -> FileRef:
    content = b"original media fixture"
    return FileRef(
        id=uuid4(), sha256=Sha256(value=sha256(content).hexdigest()), size_bytes=len(content)
    )


@pytest.fixture
def text_version() -> FeatureVersion:
    return FeatureVersion(
        model="ocr-fixture",
        weights="weights-v1",
        task_adapter="adapter-v1",
        preprocessing="sampling-v1",
    )


@pytest.fixture
def vector_version() -> FeatureVersion:
    return FeatureVersion(
        model="embedding-fixture",
        weights="weights-v1",
        task_adapter="adapter-v1",
        preprocessing="sampling-v1",
        space="compatible-space-v1",
        dimensions=2,
    )


@pytest.fixture
def extraction(
    original_file: FileRef, text_version: FeatureVersion, now: datetime
) -> FeatureExtraction:
    return FeatureExtraction(
        id=ExtractionId(uuid4()),
        media_id=uuid4(),
        source=original_file,
        cycle_id=CycleId(uuid4()),
        kind=FeatureKind.OCR,
        version=text_version,
        requested_at=now,
    )


@pytest.fixture
def task(extraction: FeatureExtraction, now: datetime) -> ProcessingTask:
    return ProcessingTask(
        id=uuid4(),
        input=TaskInput(
            extraction_id=extraction.id,
            cycle_id=extraction.cycle_id,
            source=extraction.source,
            kind=extraction.kind,
            version=extraction.version,
        ),
        created_at=now,
        attempts=Number.ZERO,
    )
