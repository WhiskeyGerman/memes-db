"""Standalone исходящий клиент и необязательный port-bound health процесс."""

import argparse
import asyncio
import logging
import signal
from enum import StrEnum

import httpx
import uvicorn
from fastapi import FastAPI

from memes_worker.application.dto.runtime import Payload
from memes_worker.application.use_cases.runner import WorkerRunner
from memes_worker.infrastructure.cache.sqlite import SQLiteCache
from memes_worker.infrastructure.config.settings import WorkerSettings
from memes_worker.infrastructure.http.client import RESTCoordinatorClient
from memes_worker.infrastructure.logging import configure_logging
from memes_worker.infrastructure.ml.executor import DecoderLimit, MediaExecutor


class WorkerCommand(StrEnum):
    """Типы независимо запускаемых процессов."""

    RUN = "run"
    CAPABILITIES = "capabilities"
    HEALTH = "health"


def create_app() -> FastAPI:
    """Создать необязательный диагностический HTTP процесс."""
    app = FastAPI(title="Memes worker")

    @app.get("/health/live", response_model=dict[str, str])
    async def liveness() -> Payload:
        """Ответ относится к health процессу, а не готовности GPU."""
        return {"status": "alive"}

    return app


async def run(settings: WorkerSettings) -> None:
    """Личный компьютер не получает доступ к основной БД."""
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(shutdown_signal, stopped.set)
        except NotImplementedError:
            signal.signal(
                shutdown_signal, lambda _signal, _frame: loop.call_soon_threadsafe(stopped.set)
            )
    capabilities = settings.capabilities()
    cache = SQLiteCache(
        settings.cache_directory,
        maximum_bytes=settings.cache_bytes,
        reservation_bytes=DecoderLimit.MAX_DISK_BYTES * settings.maximum_parallel_tasks,
    )
    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds, follow_redirects=False
    ) as client:
        runner = WorkerRunner(
            RESTCoordinatorClient(client, settings),
            MediaExecutor(client, settings),
            cache,
            capabilities,
            renewal_seconds=settings.renewal_seconds,
            cache_seconds=settings.cache_seconds,
        )
        try:
            await runner.run(stopped, concurrency=settings.maximum_parallel_tasks)
        finally:
            cache.close()


def main() -> None:
    """Credentials и адрес подключаемого координатора берутся из окружения."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", nargs="?", default=WorkerCommand.RUN, choices=list(WorkerCommand)
    )
    arguments = parser.parse_args()
    settings = WorkerSettings()
    configure_logging(level=settings.log_level, release_id=settings.release_id)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if arguments.command == WorkerCommand.CAPABILITIES:
        logging.getLogger(__name__).info("worker_capabilities %s", settings.capabilities())
    elif arguments.command == WorkerCommand.HEALTH:
        uvicorn.run(
            create_app(),
            host=settings.health_host,
            port=settings.health_port,
            log_config=None,
            access_log=False,
        )
    else:
        asyncio.run(run(settings))
