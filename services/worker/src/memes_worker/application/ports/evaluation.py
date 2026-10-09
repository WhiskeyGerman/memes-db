"""Порт чтения проверяемого набора качества."""

from abc import ABC, abstractmethod

from memes_worker.application.dto.evaluation import EvaluationDataset


class IEvaluationDataset(ABC):
    """Источники разметки могут меняться без изменения расчета."""

    @abstractmethod
    def load(self) -> EvaluationDataset:
        """Загрузить размеченный набор и реальные измерения."""
