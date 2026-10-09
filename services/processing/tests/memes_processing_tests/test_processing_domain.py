from dataclasses import replace
from datetime import datetime, timedelta
from math import inf, nan
from uuid import uuid4

import pytest

from memes_kernel.errors import DomainError
from memes_kernel.values import Number
from memes_processing.domain.features import (
    CycleId,
    Embedding,
    Embeddings,
    ExtractedText,
    ExtractionId,
    FeatureExtraction,
    FeatureKind,
    FeatureState,
    FeatureVersion,
    FeatureViolation,
    Language,
    TextFragment,
    TimeSpan,
)


def test_no_text_is_successful_empty_result(extraction: FeatureExtraction, now: datetime) -> None:
    done = extraction.complete(
        cycle_id=extraction.cycle_id, result=ExtractedText(fragments=()), now=now
    )
    assert done.state is FeatureState.EMPTY


def test_stale_cycle_cannot_publish_result(extraction: FeatureExtraction, now: datetime) -> None:
    with pytest.raises(DomainError) as error:
        extraction.complete(cycle_id=CycleId(uuid4()), result=ExtractedText(fragments=()), now=now)
    assert error.value.code is FeatureViolation.STALE_CYCLE


def test_failed_stage_does_not_change_another_stage(
    extraction: FeatureExtraction, now: datetime
) -> None:
    failed = extraction.finish_without_result(
        cycle_id=extraction.cycle_id, state=FeatureState.FAILED, now=now
    )
    separate = replace(extraction, id=ExtractionId(uuid4()), cycle_id=CycleId(uuid4()))
    done = separate.complete(
        cycle_id=separate.cycle_id,
        result=ExtractedText(fragments=(TextFragment(text="Привет", language=Language.RUSSIAN),)),
        now=now,
    )
    assert failed.state is FeatureState.FAILED
    assert done.state is FeatureState.AVAILABLE


def test_cancelled_cycle_rejects_late_result(extraction: FeatureExtraction, now: datetime) -> None:
    cancelled = extraction.finish_without_result(
        cycle_id=extraction.cycle_id, state=FeatureState.CANCELLED, now=now
    )
    with pytest.raises(DomainError) as error:
        cancelled.complete(cycle_id=cancelled.cycle_id, result=ExtractedText(fragments=()), now=now)
    assert error.value.code is FeatureViolation.CLOSED_CYCLE


@pytest.mark.parametrize("values", [(nan, 1.0), (inf, 1.0), (0.0, 0.0), (1.0,)])
def test_embedding_rejects_invalid_values(
    vector_version: FeatureVersion, values: tuple[float, ...]
) -> None:
    with pytest.raises(DomainError) as error:
        Embedding(version=vector_version, values=values)
    assert error.value.code is FeatureViolation.INVALID_VECTOR


def test_cross_modal_comparison_requires_explicit_compatible_space(
    vector_version: FeatureVersion,
) -> None:
    assert vector_version.dimensions is not None
    text_model = replace(vector_version, model="compatible-text-model")
    assert text_model.compatible_with(vector_version)
    assert not replace(vector_version, space="other-space").compatible_with(vector_version)
    assert not replace(vector_version, dimensions=vector_version.dimensions + 1).compatible_with(
        vector_version
    )


def test_transcription_requires_time_marks(extraction: FeatureExtraction, now: datetime) -> None:
    transcript = replace(extraction, kind=FeatureKind.TRANSCRIPT)
    fragment = TextFragment(text="hello", language=Language.ENGLISH)
    with pytest.raises(DomainError) as error:
        transcript.complete(
            cycle_id=transcript.cycle_id, result=ExtractedText(fragments=(fragment,)), now=now
        )
    assert error.value.code is FeatureViolation.WRONG_RESULT
    timed = replace(fragment, span=TimeSpan(start=timedelta(), end=timedelta(seconds=Number.ONE)))
    complete = transcript.complete(
        cycle_id=transcript.cycle_id, result=ExtractedText(fragments=(timed,)), now=now
    )
    assert complete.state is FeatureState.AVAILABLE


def test_result_with_wrong_model_version_is_rejected(
    extraction: FeatureExtraction,
    vector_version: FeatureVersion,
    now: datetime,
) -> None:
    visual = replace(extraction, kind=FeatureKind.VISUAL_EMBEDDING, version=vector_version)
    wrong = Embedding(version=replace(vector_version, weights="other-weights"), values=(1.0, 0.5))
    with pytest.raises(DomainError) as error:
        visual.complete(cycle_id=visual.cycle_id, result=Embeddings(items=(wrong,)), now=now)
    assert error.value.code is FeatureViolation.WRONG_RESULT
