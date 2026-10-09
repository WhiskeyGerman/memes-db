"""Расчет качества и калибровка порогов на строго отдельной части."""

from collections.abc import Sequence
from math import isfinite, log2
from unicodedata import normalize

from memes_worker.application.dto.evaluation import (
    EvaluationDataset,
    EvaluationLimit,
    EvaluationSplit,
    PairMeasurement,
    QueryMeasurement,
    TextMeasure,
)
from memes_worker.application.dto.runtime import JSONValue, Payload
from memes_worker.application.ports.evaluation import IEvaluationDataset


def edit_distance(reference: Sequence[str], prediction: Sequence[str]) -> int:
    """Вычислить расстояние Левенштейна с линейным расходом памяти."""
    previous = list(range(len(prediction) + 1))
    for row, expected in enumerate(reference, start=1):
        current = [row]
        for column, actual in enumerate(prediction, start=1):
            current.append(
                min(
                    previous[column] + 1,
                    current[column - 1] + 1,
                    previous[column - 1] + int(expected != actual),
                )
            )
        previous = current
    return previous[-1]


def ndcg(query: QueryMeasurement) -> float:
    """Оценить первые десять позиций по шкале релевантности PRD."""
    actual = sum(
        (2 ** query.relevance.get(item, 0) - 1) / log2(index + 2)
        for index, item in enumerate(query.ranking[: EvaluationLimit.CANDIDATES])
    )
    ideal = sum(
        (2**grade - 1) / log2(index + 2)
        for index, grade in enumerate(
            sorted(query.relevance.values(), reverse=True)[: EvaluationLimit.CANDIDATES]
        )
    )
    return actual / ideal if ideal else 0.0


def pair_metrics(pairs: Sequence[PairMeasurement], threshold: float) -> Payload:
    """Измерить положительные пары и precision реально показанных кандидатов."""
    positives = sum(pair.positive for pair in pairs)
    shown = [
        pair
        for pair in pairs
        if pair.rank <= EvaluationLimit.CANDIDATES and pair.similarity >= threshold
    ]
    found = sum(pair.positive for pair in shown)
    return {
        "pairs": len(pairs),
        "positive_pairs": positives,
        "shown_candidates": len(shown),
        "recall_at_10": found / positives if positives else None,
        "precision": found / len(shown) if shown else None,
    }


def calibrate(pairs: Sequence[PairMeasurement]) -> float:
    """Выбрать порог отдельно по модальности, используя только калибровку."""
    if not pairs or not any(pair.positive for pair in pairs):
        raise ValueError("calibration_requires_positive_pairs")
    target = EvaluationLimit.PRECISION_PERCENT / EvaluationLimit.PERCENT_SCALE
    candidates: list[tuple[float, float, float]] = []
    for threshold in sorted({pair.similarity for pair in pairs}):
        metrics = pair_metrics(pairs, threshold)
        precision = metrics["precision"]
        recall = metrics["recall_at_10"]
        if isinstance(precision, float) and isinstance(recall, float) and precision >= target:
            candidates.append((recall, precision, threshold))
    if not candidates:
        raise ValueError("calibration_cannot_meet_precision_target")
    return max(candidates)[-1]


def validate(dataset: EvaluationDataset, *, require_prd_size: bool) -> None:
    """Запретить утечку семейств между подбором порога и финальной оценкой."""
    families: dict[EvaluationSplit, set[str]] = {split: set() for split in EvaluationSplit}
    seen: set[tuple[str, str]] = set()
    for pair in dataset.pairs:
        if (
            not isfinite(pair.similarity)
            or not -1 <= pair.similarity <= 1
            or pair.rank < 1
            or not pair.source_family
            or not pair.candidate_family
        ):
            raise ValueError("invalid_pair_measurement")
        identity = pair.source_id, pair.candidate_id
        if identity in seen:
            raise ValueError("duplicate_pair_measurement")
        seen.add(identity)
        families[pair.split].update((pair.source_family, pair.candidate_family))
    if families[EvaluationSplit.CALIBRATION] & families[EvaluationSplit.EVALUATION]:
        raise ValueError("calibration_evaluation_family_leak")
    query_ids: set[str] = set()
    for query in dataset.queries:
        if query.id in query_ids or len(set(query.ranking)) != len(query.ranking):
            raise ValueError("duplicate_query_or_ranking")
        query_ids.add(query.id)
        if not query.relevance or any(
            not 0 <= grade <= EvaluationLimit.MAXIMUM_RELEVANCE
            for grade in query.relevance.values()
        ):
            raise ValueError("invalid_relevance")
    if require_prd_size and (
        len(dataset.pairs) < EvaluationLimit.MINIMUM_PAIRS
        or len(dataset.queries) < EvaluationLimit.MINIMUM_QUERIES
    ):
        raise ValueError("dataset_below_prd_minimum")


def evaluate(source: IEvaluationDataset, *, require_prd_size: bool) -> Payload:
    """Сформировать честный отчет; отсутствующие измерения остаются отсутствующими."""
    dataset = source.load()
    validate(dataset, require_prd_size=require_prd_size)
    thresholds: Payload = {}
    pairs: Payload = {}
    for modality in sorted({pair.modality for pair in dataset.pairs}):
        calibration = [
            pair
            for pair in dataset.pairs
            if pair.modality == modality and pair.split is EvaluationSplit.CALIBRATION
        ]
        threshold = calibrate(calibration)
        thresholds[modality] = threshold
        selected = [
            pair
            for pair in dataset.pairs
            if pair.modality == modality and pair.split is EvaluationSplit.EVALUATION
        ]
        breakdown: Payload = {}
        for language in sorted({pair.language for pair in selected}):
            breakdown[language] = pair_metrics(
                [pair for pair in selected if pair.language == language], threshold
            )
        pairs[modality] = {"overall": pair_metrics(selected, threshold), "languages": breakdown}
    query_breakdown: Payload = {}
    for label in sorted({f"{query.language}/{query.media_kind}" for query in dataset.queries}):
        values = [
            ndcg(query)
            for query in dataset.queries
            if f"{query.language}/{query.media_kind}" == label
        ]
        query_breakdown[label] = {"queries": len(values), "ndcg_at_10": sum(values) / len(values)}
    text_metrics: Payload = {}
    for kind in TextMeasure:
        for label in sorted(
            {f"{item.language}/{item.media_kind}" for item in dataset.texts if item.kind is kind}
        ):
            reference_units = 0
            errors = 0
            count = 0
            for item in dataset.texts:
                if item.kind is not kind or f"{item.language}/{item.media_kind}" != label:
                    continue
                reference = normalize("NFC", item.reference)
                prediction = normalize("NFC", item.prediction)
                expected = list(reference) if kind is TextMeasure.OCR else reference.split()
                actual = list(prediction) if kind is TextMeasure.OCR else prediction.split()
                reference_units += len(expected)
                errors += edit_distance(expected, actual)
                count += 1
            text_metrics[f"{kind.value}/{label}"] = {
                "samples": count,
                "reference_units": reference_units,
                "edits": errors,
                "cer" if kind is TextMeasure.OCR else "wer": errors / reference_units
                if reference_units
                else None,
            }
    ndcg_value: JSONValue = (
        sum(ndcg(query) for query in dataset.queries) / len(dataset.queries)
        if dataset.queries
        else None
    )
    return {
        "dataset_pairs": len(dataset.pairs),
        "dataset_queries": len(dataset.queries),
        "prd_dataset_size_met": len(dataset.pairs) >= EvaluationLimit.MINIMUM_PAIRS
        and len(dataset.queries) >= EvaluationLimit.MINIMUM_QUERIES,
        "thresholds": thresholds,
        "pair_metrics": pairs,
        "query_metrics": {"ndcg_at_10": ndcg_value, "breakdown": query_breakdown},
        "text_metrics": text_metrics,
        "normalization": "Unicode NFC; case and punctuation preserved; WER splits whitespace",
    }
