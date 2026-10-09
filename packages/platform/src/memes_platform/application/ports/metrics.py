"""Порт получения эксплуатационного снимка своего сервиса."""

from abc import ABC, abstractmethod

from memes_platform.application.dto.metrics import MetricSample


class IMetricsProvider(ABC):
    """Снимок внешнего состояния без изменения рабочих данных."""

    @abstractmethod
    async def snapshot(self) -> tuple[MetricSample, ...]:
        """Получить конечный набор агрегированных показателей."""
