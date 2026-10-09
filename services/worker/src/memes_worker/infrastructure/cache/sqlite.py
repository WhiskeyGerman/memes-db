"""Удаляемый SQLite-кэш с неизменяемым TTL и дисковым пределом."""

import asyncio
import json
import shutil
import sqlite3
from datetime import datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from uuid import UUID

from memes_worker.application.dto.runtime import WorkerExecutionError, WorkerFailure, WorkResult
from memes_worker.application.execution import ExecutionKey
from memes_worker.application.ports.execution import IResultCache
from memes_worker.infrastructure.wire import object_payload


class CacheLimit(IntEnum):
    """Максимальное число имен в одном heartbeat."""

    SOURCES = 1024


class CacheName(StrEnum):
    """Фиксированные локальные артефакты."""

    DATABASE = "results.sqlite3"
    WORK_DIRECTORY = "work"
    OWNERSHIP = "ownership.sqlite3"


def key_text(key: ExecutionKey) -> str:
    """Полный ключ включает источник, цикл, фрагмент и обе версии."""
    return json.dumps(
        {
            "source_id": str(key.source_id),
            "sha256": key.source_hash.value,
            "cycle_id": str(key.cycle_id),
            "operation": key.operation.value,
            "fragment_id": str(key.fragment_id),
            "model": key.model_revision,
            "preprocessing": key.preprocessing_revision,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class SQLiteCache(IResultCache):
    """Потеря всей директории допустима и не теряет серверное состояние."""

    def __init__(self, directory: Path, *, maximum_bytes: int, reservation_bytes: int = 0) -> None:
        """Создать журнал лишь внутри назначенной рабочей директории."""
        directory = directory.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        self.reservation_bytes = reservation_bytes
        self.lock = asyncio.Lock()
        self.ownership = sqlite3.connect(
            directory / CacheName.OWNERSHIP,
            timeout=0,
            check_same_thread=False,
        )
        try:
            self.ownership.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as error:
            self.ownership.close()
            raise WorkerExecutionError(WorkerFailure.RESOURCE_EXHAUSTED) from error
        work = directory / CacheName.WORK_DIRECTORY
        if work.exists():
            if not work.resolve().is_relative_to(directory):
                raise WorkerExecutionError(WorkerFailure.INVALID_INPUT)
            shutil.rmtree(work)
        self.directory = directory
        self.maximum_bytes = maximum_bytes
        self.database = directory / CacheName.DATABASE
        self._setup()

    def _setup(self) -> None:
        """Определить изолированную структуру удаляемого журнала."""
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS results (cache_key TEXT PRIMARY KEY, "
                "source_id TEXT NOT NULL, payload TEXT NOT NULL, "
                "preview BLOB, expires_at REAL NOT NULL)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS ix_results_ttl ON results(expires_at)")

    async def get(self, key: ExecutionKey, *, now: datetime) -> WorkResult | None:
        """Просроченный результат не читается даже до фоновой очистки."""

        def read() -> WorkResult | None:
            with sqlite3.connect(self.database) as connection:
                row = connection.execute(
                    "SELECT payload, preview FROM results WHERE cache_key=? AND expires_at>?",
                    (key_text(key), now.timestamp()),
                ).fetchone()
                if row is None:
                    return None
                return WorkResult(
                    key=key, payload=object_payload(json.loads(row[0])), preview=row[1]
                )

        async with self.lock:
            return await asyncio.to_thread(read)

    async def put(self, result: WorkResult, *, expires_at: datetime) -> None:
        """Повторная запись не продлевает первоначальный срок жизни."""

        def write() -> None:
            with sqlite3.connect(self.database) as connection:
                connection.execute(
                    "INSERT INTO results(cache_key,source_id,payload,preview,expires_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(cache_key) DO NOTHING",
                    (
                        key_text(result.key),
                        str(result.key.source_id),
                        json.dumps(result.payload, allow_nan=False),
                        result.preview,
                        expires_at.timestamp(),
                    ),
                )

        async with self.lock:
            await asyncio.to_thread(write)

    async def purge(self, *, now: datetime) -> None:
        """Освободить записи и страницы; при старте обрабатывается старый TTL."""

        def cleanup() -> None:
            with sqlite3.connect(self.database) as connection:
                connection.execute("DELETE FROM results WHERE expires_at<=?", (now.timestamp(),))
                connection.commit()
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("PRAGMA incremental_vacuum")

        async with self.lock:
            await asyncio.to_thread(cleanup)

    async def revoke(self, source_id: UUID) -> None:
        """Удаление оригинала удаляет все его циклы из локального кэша."""

        def delete() -> None:
            with sqlite3.connect(self.database) as connection:
                connection.execute("DELETE FROM results WHERE source_id=?", (str(source_id),))

        async with self.lock:
            await asyncio.to_thread(delete)

    async def source_ids(self) -> tuple[UUID, ...]:
        """Передать лишь идентификаторы, без локальных результатов и файлов."""

        def read() -> tuple[UUID, ...]:
            with sqlite3.connect(self.database) as connection:
                rows = connection.execute(
                    "SELECT DISTINCT source_id FROM results LIMIT ?", (CacheLimit.SOURCES,)
                ).fetchall()
                return tuple(UUID(row[0]) for row in rows)

        async with self.lock:
            return await asyncio.to_thread(read)

    async def full(self) -> bool:
        """Учитывать SQLite, WAL, входы и промежуточные файлы вместе."""

        def usage() -> int:
            total = 0
            for path in self.directory.rglob("*"):
                try:
                    if path.is_file():
                        total += path.stat().st_size
                except FileNotFoundError:
                    continue
            return total

        return await asyncio.to_thread(usage) >= self.maximum_bytes - self.reservation_bytes

    def close(self) -> None:
        """Освободить межпроцессное владение удаляемым кэшем."""
        self.ownership.close()
