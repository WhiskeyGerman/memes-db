"""Ограниченное выполнение известных программ без shell."""

import asyncio
import logging
import os
import signal
import sys
from collections.abc import Sequence
from enum import IntEnum, StrEnum
from pathlib import Path

from memes_worker.application.dto.runtime import WorkerExecutionError, WorkerFailure
from memes_worker.application.execution import Operation
from memes_worker.infrastructure.ml.diagnostics import (
    DiagnosticCode,
    DiagnosticException,
    ProcessDiagnostic,
    ProcessProgram,
    drain_diagnostic,
)
from memes_worker.infrastructure.ml.plugin_runtime import PluginExit


class ProcessLimit(IntEnum):
    """Ограничения управления дочерним процессом."""

    OUTPUT_BYTES = 8388608
    KILL_SECONDS = 5
    CHUNK_BYTES = 65536
    SIGKILL_EXIT = -9


class ProcessLog(StrEnum):
    """Фиксированный журнал без argv, путей и сырого stderr."""

    FAILED = (
        "worker_process_failed program=%s operation=%s exit_code=%s diagnostic=%s exception_type=%s"
    )
    NO_OPERATION = "none"


logger = logging.getLogger(__name__)


def log_failure(
    program: ProcessProgram,
    operation: Operation | None,
    exit_code: int | None,
    diagnostic: ProcessDiagnostic,
) -> None:
    """Записать только известные метаданные и причину отказа."""
    logger.warning(
        ProcessLog.FAILED,
        program.value,
        operation.value if operation is not None else ProcessLog.NO_OPERATION.value,
        exit_code,
        diagnostic.code.value,
        diagnostic.exception_type.value,
    )


async def execute(
    arguments: Sequence[str],
    *,
    timeout_seconds: float,
    directory: Path,
    program: ProcessProgram = ProcessProgram.UNKNOWN,
    operation: Operation | None = None,
) -> bytes:
    """Ограничить процесс, осушая stdout/stderr одновременно."""
    try:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=directory,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=sys.platform != "win32",
        )
    except OSError as error:
        log_failure(
            program,
            operation,
            None,
            ProcessDiagnostic(DiagnosticCode.CONFIGURATION, DiagnosticException.OS),
        )
        raise WorkerExecutionError(WorkerFailure.TEMPORARY) from error
    try:

        async def collect() -> bytes:
            if process.stdout is None:
                raise WorkerExecutionError(WorkerFailure.TEMPORARY)
            chunks = bytearray()
            while True:
                chunk = await process.stdout.read(ProcessLimit.CHUNK_BYTES)
                if not chunk:
                    break
                chunks.extend(chunk)
                if len(chunks) > ProcessLimit.OUTPUT_BYTES:
                    raise WorkerExecutionError(WorkerFailure.RESOURCE_EXHAUSTED)
            await process.wait()
            return bytes(chunks)

        output, diagnostic = await asyncio.wait_for(
            asyncio.gather(collect(), drain_diagnostic(process.stderr)),
            timeout=timeout_seconds,
        )
        if process.returncode != 0:
            log_failure(program, operation, process.returncode, diagnostic)
        if process.returncode in {PluginExit.RESOURCE_EXHAUSTED, ProcessLimit.SIGKILL_EXIT}:
            raise WorkerExecutionError(WorkerFailure.RESOURCE_EXHAUSTED)
        if (
            process.returncode == PluginExit.INVALID_INPUT
            and diagnostic.code is DiagnosticCode.INPUT
        ):
            raise WorkerExecutionError(WorkerFailure.INVALID_INPUT)
        if process.returncode != 0:
            raise WorkerExecutionError(WorkerFailure.TEMPORARY)
        return output
    except TimeoutError as error:
        log_failure(
            program,
            operation,
            process.returncode,
            ProcessDiagnostic(DiagnosticCode.RESOURCE),
        )
        raise WorkerExecutionError(WorkerFailure.RESOURCE_EXHAUSTED) from error
    finally:
        signal_process_tree(process, force=False)
        try:
            await asyncio.wait_for(process.wait(), timeout=ProcessLimit.KILL_SECONDS)
        except TimeoutError:
            signal_process_tree(process, force=True)
            await process.wait()
        finally:
            signal_process_tree(process, force=True)


def signal_process_tree(process: asyncio.subprocess.Process, *, force: bool) -> None:
    """Завершить отдельную POSIX process group вместе с её потомками."""
    try:
        if sys.platform != "win32":
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif process.returncode is None:
            if force:
                process.kill()
            else:
                process.terminate()
    except ProcessLookupError:
        return
