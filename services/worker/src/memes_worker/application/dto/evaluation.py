"""Разметка и измеренные результаты отдельного административного прогона."""

from dataclasses import dataclass
from enum import IntEnum, StrEnum


class EvaluationSplit(StrEnum):
    """Проверочная часть не используется для подбора порога."""

    CALIBRATION = "calibration"
    EVALUATION = "evaluation"


class TextMeasure(StrEnum):
    """Отдельные метрики извлечения текста."""

    OCR = "ocr"
    TRANSCRIPT = "transcript"


class EvaluationLimit(IntEnum):
    """Проверяемые критерии PRD."""

    CANDIDATES = 10
    MINIMUM_PAIRS = 500
    MINIMUM_QUERIES = 200
    PRECISION_PERCENT = 70
    RECALL_PERCENT = 90
    NDCG_PERCENT = 75
    PERCENT_SCALE = 100
    MAXIMUM_RELEVANCE = 3
    MAXIMUM_DATASET_BYTES = 104857600


@dataclass(frozen=True, slots=True, kw_only=True)
class PairMeasurement:
    """Одна размеченная пара с фактическим cosine и позицией кандидата."""

    source_id: str
    candidate_id: str
    source_family: str
    candidate_family: str
    modality: str
    language: str
    split: EvaluationSplit
    positive: bool
    similarity: float
    rank: int


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryMeasurement:
    """Фактический рейтинг ответа и независимая оценка релевантности."""

    id: str
    language: str
    media_kind: str
    relevance: dict[str, int]
    ranking: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class TextMeasurement:
    """Эталонный и фактически распознанный текст."""

    kind: TextMeasure
    language: str
    media_kind: str
    reference: str
    prediction: str


@dataclass(frozen=True, slots=True, kw_only=True)
class EvaluationDataset:
    """Внешние фактические измерения, а не автоматически созданная разметка."""

    pairs: tuple[PairMeasurement, ...]
    queries: tuple[QueryMeasurement, ...]
    texts: tuple[TextMeasurement, ...]
