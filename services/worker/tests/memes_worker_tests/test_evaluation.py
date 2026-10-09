from dataclasses import replace
from math import log2

import pytest

from memes_worker.application.dto.evaluation import EvaluationDataset
from memes_worker.application.use_cases.evaluation import evaluate, validate
from memes_worker.infrastructure.evaluation.json import JSONEvaluationDataset


def test_quality_metrics_use_held_out_data(quality_dataset: JSONEvaluationDataset) -> None:
    report = evaluate(quality_dataset, require_prd_size=False)
    assert report["prd_dataset_size_met"] is False
    assert report["thresholds"] == {"image": 0.8}
    pairs = report["pair_metrics"]
    assert isinstance(pairs, dict)
    overall = pairs["image"]
    assert isinstance(overall, dict)
    metrics = overall["overall"]
    assert isinstance(metrics, dict)
    assert metrics["precision"] == 0.5
    assert metrics["recall_at_10"] == 1.0
    queries = report["query_metrics"]
    assert isinstance(queries, dict)
    assert queries["ndcg_at_10"] == pytest.approx(1 / log2(3))
    texts = report["text_metrics"]
    assert isinstance(texts, dict)
    assert texts["ocr/ru/image"] == {
        "samples": 1,
        "reference_units": 3,
        "edits": 1,
        "cer": 1 / 3,
    }
    assert texts["transcript/en/audio"] == {
        "samples": 1,
        "reference_units": 2,
        "edits": 1,
        "wer": 0.5,
    }


def test_calibration_rejects_family_leak(quality_dataset: JSONEvaluationDataset) -> None:
    dataset = quality_dataset.load()
    leaked = replace(dataset.pairs[-1], source_family=dataset.pairs[0].source_family)
    with pytest.raises(ValueError, match="family_leak"):
        validate(replace(dataset, pairs=(*dataset.pairs[:-1], leaked)), require_prd_size=False)


def test_small_dataset_cannot_claim_prd_acceptance(quality_dataset: JSONEvaluationDataset) -> None:
    with pytest.raises(ValueError, match="below_prd_minimum"):
        evaluate(quality_dataset, require_prd_size=True)


def test_invalid_relevance_is_rejected(quality_dataset: JSONEvaluationDataset) -> None:
    dataset = quality_dataset.load()
    invalid_query = replace(dataset.queries[0], relevance={"a": 4})
    invalid = EvaluationDataset(
        pairs=dataset.pairs,
        queries=(invalid_query,),
        texts=dataset.texts,
    )
    with pytest.raises(ValueError, match="invalid_relevance"):
        validate(invalid, require_prd_size=False)
