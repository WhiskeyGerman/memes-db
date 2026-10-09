"""Ограниченные SQL-агрегаты только принадлежащей процессу базы."""

from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from memes_platform.application.dto.metrics import MetricName, MetricSample
from memes_platform.application.ports.metrics import IMetricsProvider


class PostgreSQLMetrics(IMetricsProvider):
    """Одна короткая read-only транзакция эксплуатационного снимка."""

    def __init__(self, engine: AsyncEngine, queries: Mapping[MetricName, str]) -> None:
        """Сохранить фиксированные запросы владельца таблиц.

        Args:
            engine: Пул только своего сервиса.
            queries: Проверенные SQL без пользовательского ввода.
        """
        self.engine = engine
        self.queries = dict(queries)

    async def snapshot(self) -> tuple[MetricSample, ...]:
        """Выполнить агрегаты с конечным серверным statement timeout."""
        samples: list[MetricSample] = []
        async with self.engine.begin() as connection:
            await connection.execute(text("SET LOCAL statement_timeout = '2s'"))
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            for name, query in self.queries.items():
                value = await connection.scalar(text(query))
                samples.append(MetricSample(name=name, value=float(value or 0)))
        return tuple(samples)
