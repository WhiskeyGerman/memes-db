import asyncio
import errno
import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from memes_worker.application.dto.runtime import WorkAssignment, WorkerExecutionError, WorkerFailure
from memes_worker.application.execution import Operation
from memes_worker.infrastructure.config.settings import WorkerSettings
from memes_worker.infrastructure.ml.diagnostics import (
    DiagnosticCode,
    DiagnosticException,
    DiagnosticLimit,
    DiagnosticMarker,
    PluginInputError,
    PluginModelConfigurationError,
    ProcessDiagnostic,
    ProcessProgram,
    diagnostic_line,
    drain_diagnostic,
    parse_diagnostic,
)
from memes_worker.infrastructure.ml.executor import MediaExecutor
from memes_worker.infrastructure.ml.plugin_runtime import PluginExit, entrypoint
from memes_worker.infrastructure.ml.process import execute


@pytest.mark.parametrize(
    ("error_class", "exit_code", "diagnostic"),
    [
        (PluginInputError, PluginExit.INVALID_INPUT, DiagnosticCode.INPUT),
        (PluginModelConfigurationError, PluginExit.TEMPORARY, DiagnosticCode.CONFIGURATION),
        (ValueError, PluginExit.TEMPORARY, DiagnosticCode.RUNTIME),
        (TypeError, PluginExit.TEMPORARY, DiagnosticCode.RUNTIME),
        (OSError, PluginExit.TEMPORARY, DiagnosticCode.RUNTIME),
        (ImportError, PluginExit.TEMPORARY, DiagnosticCode.RUNTIME),
        (MemoryError, PluginExit.RESOURCE_EXHAUSTED, DiagnosticCode.RESOURCE),
    ],
)
def test_plugin_reports_only_allowlisted_error_metadata(
    error_class: type[Exception],
    exit_code: PluginExit,
    diagnostic: DiagnosticCode,
    sensitive_error: str,
    raising_action: Callable[[BaseException], Callable[[], None]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        entrypoint(raising_action(error_class(sensitive_error)))
    assert error.value.code == exit_code
    captured = capsys.readouterr()
    assert sensitive_error not in captured.err
    assert captured.out == ""
    parsed = parse_diagnostic(captured.err.rstrip().encode())
    assert parsed is not None and parsed.code is diagnostic


@pytest.mark.parametrize(
    "line",
    [
        b"SDK traceback contains private input",
        DiagnosticMarker.PREFIX.encode()
        + b'{"code":"PRIVATE_QUERY","exception_type":"ValueError"}',
        DiagnosticMarker.PREFIX.encode() + b'{"code":"runtime","exception_type":"PrivateClass"}',
        DiagnosticMarker.PREFIX.encode()
        + b'{"code":"runtime","exception_type":"ValueError","detail":"PRIVATE_QUERY"}',
        DiagnosticMarker.PREFIX.encode() + b"a" * DiagnosticLimit.LINE_BYTES,
    ],
)
def test_diagnostic_parser_rejects_unknown_or_unbounded_fields(line: bytes) -> None:
    assert parse_diagnostic(line) is None


@pytest.mark.asyncio
async def test_stderr_is_drained_beyond_budget_without_accepting_late_metadata() -> None:
    stream = asyncio.StreamReader()
    stream.feed_data(b"x" * DiagnosticLimit.BYTES + b"\n")
    stream.feed_data(
        diagnostic_line(ProcessDiagnostic(DiagnosticCode.INPUT, DiagnosticException.INPUT)).encode()
        + b"\n"
    )
    stream.feed_eof()
    assert await drain_diagnostic(stream) == ProcessDiagnostic()
    assert stream.at_eof()


@pytest.mark.asyncio
async def test_split_diagnostic_line_is_parsed() -> None:
    stream = asyncio.StreamReader()
    expected = ProcessDiagnostic(DiagnosticCode.RUNTIME, DiagnosticException.TYPE)
    line = diagnostic_line(expected).encode() + b"\n"
    task = asyncio.create_task(drain_diagnostic(stream))
    for byte in line:
        stream.feed_data(bytes([byte]))
        await asyncio.sleep(0)
    stream.feed_eof()
    assert await task == expected


@pytest.mark.asyncio
async def test_flooded_stderr_does_not_deadlock_or_enter_logs(
    subprocess_diagnostic_script: Path,
    sensitive_error: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    result = await execute(
        [sys.executable, str(subprocess_diagnostic_script), "flood", sensitive_error],
        timeout_seconds=10,
        directory=tmp_path,
        program=ProcessProgram.OCR,
        operation=Operation.OCR,
    )
    assert result == b"ok"
    assert sensitive_error not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "failure", "diagnostic"),
    [
        ("input", WorkerFailure.INVALID_INPUT, DiagnosticCode.INPUT),
        ("untyped", WorkerFailure.TEMPORARY, DiagnosticCode.UNKNOWN),
    ],
)
async def test_only_trusted_input_diagnostic_is_terminal(
    mode: str,
    failure: WorkerFailure,
    diagnostic: DiagnosticCode,
    subprocess_diagnostic_script: Path,
    sensitive_error: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    with pytest.raises(WorkerExecutionError) as error:
        await execute(
            [sys.executable, str(subprocess_diagnostic_script), mode, sensitive_error],
            timeout_seconds=10,
            directory=tmp_path,
            program=ProcessProgram.OCR,
            operation=Operation.OCR,
        )
    assert error.value.reason is failure
    assert f"diagnostic={diagnostic.value}" in caplog.text
    assert "program=memes_worker.infrastructure.ml.ocr" in caplog.text
    assert sensitive_error not in caplog.text
    assert str(tmp_path) not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_error", "reason"),
    [
        (FileNotFoundError(errno.ENOENT, "response_missing"), WorkerFailure.TEMPORARY),
        (OSError(errno.ENOSPC, "private_disk"), WorkerFailure.RESOURCE_EXHAUSTED),
        (PermissionError(errno.EACCES, "private_path"), WorkerFailure.TEMPORARY),
        (json.JSONDecodeError("private_response", "private", 0), WorkerFailure.TEMPORARY),
        (WorkerExecutionError(WorkerFailure.INVALID_INPUT), WorkerFailure.INVALID_INPUT),
    ],
)
async def test_executor_preserves_input_validation_and_retries_local_failures(
    runtime_error: Exception,
    reason: WorkerFailure,
    worker_configuration: dict[str, object],
    active_work: WorkAssignment,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    settings = WorkerSettings.model_validate(worker_configuration)
    async with httpx.AsyncClient() as client:
        executor = MediaExecutor(client, settings)
        monkeypatch.setattr(executor, "_download", AsyncMock())
        monkeypatch.setattr(executor, "_operation", AsyncMock(side_effect=runtime_error))
        with pytest.raises(WorkerExecutionError) as error:
            await executor.execute(active_work)
    assert error.value.reason is reason
