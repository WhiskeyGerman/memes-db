"""Проверка JSON-файла с разметкой и фактическими результатами."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from memes_worker.application.dto.evaluation import (
    EvaluationDataset,
    EvaluationLimit,
    EvaluationSplit,
    PairMeasurement,
    QueryMeasurement,
    TextMeasure,
    TextMeasurement,
)
from memes_worker.application.ports.evaluation import IEvaluationDataset


class PairSchema(BaseModel):
    """Обязательные поля пары и семейство каждого оригинала."""

    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    source_family: str = Field(min_length=1)
    candidate_family: str = Field(min_length=1)
    modality: str = Field(min_length=1)
    language: str = Field(pattern="^(ru|en|none)$")
    split: EvaluationSplit
    positive: bool
    similarity: float = Field(ge=-1, le=1)
    rank: int = Field(ge=1)


class QuerySchema(BaseModel):
    """Разметка релевантности и реально полученный порядок результатов."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    language: str = Field(pattern="^(ru|en)$")
    media_kind: str = Field(min_length=1)
    relevance: dict[str, int]
    ranking: tuple[str, ...]


class TextSchema(BaseModel):
    """Фактически распознанный текст без вымышленных результатов."""

    model_config = ConfigDict(extra="forbid")
    kind: TextMeasure
    language: str = Field(pattern="^(ru|en)$")
    media_kind: str = Field(min_length=1)
    reference: str
    prediction: str


class DatasetSchema(BaseModel):
    """Набор хранится отдельно от runtime и модельных весов."""

    model_config = ConfigDict(extra="forbid")
    pairs: tuple[PairSchema, ...]
    queries: tuple[QuerySchema, ...]
    texts: tuple[TextSchema, ...] = ()


class JSONEvaluationDataset(IEvaluationDataset):
    """Обработчик воспроизводимого внешнего измерения."""

    def __init__(self, path: Path) -> None:
        """Принять путь административного входа."""
        self.path = path

    def load(self) -> EvaluationDataset:
        """Отклонить слишком большой либо неполный файл до расчета."""
        if self.path.stat().st_size > EvaluationLimit.MAXIMUM_DATASET_BYTES:
            raise ValueError("evaluation_dataset_too_large")
        payload = DatasetSchema.model_validate_json(self.path.read_bytes())
        return EvaluationDataset(
            pairs=tuple(PairMeasurement(**pair.model_dump()) for pair in payload.pairs),
            queries=tuple(QueryMeasurement(**query.model_dump()) for query in payload.queries),
            texts=tuple(TextMeasurement(**text.model_dump()) for text in payload.texts),
        )
