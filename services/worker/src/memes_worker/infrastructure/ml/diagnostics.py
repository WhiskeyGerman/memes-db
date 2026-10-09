"""Ограниченный диагностический контракт без содержимого медиа и запросов."""

import asyncio
import json
from dataclasses import dataclass
from enum import IntEnum, StrEnum


class DiagnosticLimit(IntEnum):
    """Бюджеты чтения stderr независимо от объёма вывода SDK."""

    BYTES = 65536
    LINE_BYTES = 4096
    CHUNK_BYTES = 4096


class DiagnosticMarker(StrEnum):
    """Зарезервированный префикс доверенного plugin runtime."""

    PREFIX = "MEMES_WORKER_DIAGNOSTIC "


class DiagnosticMessage(StrEnum):
    """Фиксированные внутренние сообщения исключений адаптеров."""

    INVALID_MODEL_INPUT = "invalid_model_input"
    INVALID_LOCAL_MODEL_ARTIFACT = "invalid_local_model_artifact"


class DiagnosticCode(StrEnum):
    """Только заранее определённые причины без текстов исключений."""

    UNKNOWN = "unknown"
    INPUT = "input"
    CONFIGURATION = "model_configuration"
    RUNTIME = "runtime"
    RESOURCE = "resource"


class DiagnosticException(StrEnum):
    """Фиксированные классы исключений вместо произвольных имён SDK."""

    UNKNOWN = "unknown"
    INPUT = "PluginInputError"
    CONFIGURATION = "PluginModelConfigurationError"
    WORKER = "WorkerExecutionError"
    TRANSPORT = "WorkerTransportError"
    MEMORY = "MemoryError"
    RUNTIME = "RuntimeError"
    VALUE = "ValueError"
    TYPE = "TypeError"
    OS = "OSError"
    ASSERTION = "AssertionError"
    IMPORT = "ImportError"


class ProcessProgram(StrEnum):
    """Известные исполняемые программы; argv и пути не попадают в журнал."""

    UNKNOWN = "unknown"
    FFMPEG = "ffmpeg"
    FFPROBE = "ffprobe"
    IMAGES = "memes_worker.infrastructure.ml.images"
    OCR = "memes_worker.infrastructure.ml.ocr"
    ASR = "memes_worker.infrastructure.ml.asr"
    EMBEDDING = "memes_worker.infrastructure.ml.embedding"


@dataclass(frozen=True, slots=True)
class ProcessDiagnostic:
    """Безопасные поля одной диагностической записи."""

    code: DiagnosticCode = DiagnosticCode.UNKNOWN
    exception_type: DiagnosticException = DiagnosticException.UNKNOWN


class PluginInputError(ValueError):
    """Заведомо недопустимый вход, проверенный собственным адаптером."""


class PluginModelConfigurationError(ValueError):
    """Модель либо runtime не соответствуют предварительно объявленному контракту."""


def diagnostic_line(diagnostic: ProcessDiagnostic) -> str:
    """Сериализовать только enum-поля; исходное исключение не принимается."""
    return DiagnosticMarker.PREFIX + json.dumps(
        {"code": diagnostic.code.value, "exception_type": diagnostic.exception_type.value}
    )


def parse_diagnostic(line: bytes) -> ProcessDiagnostic | None:
    """Принять только ограниченный JSON с известными значениями и точной схемой."""
    prefix = DiagnosticMarker.PREFIX.encode()
    if len(line) > DiagnosticLimit.LINE_BYTES or not line.startswith(prefix):
        return None
    try:
        payload: object = json.loads(line[len(prefix) :])
        if not isinstance(payload, dict) or set(payload) != {"code", "exception_type"}:
            return None
        return ProcessDiagnostic(
            code=DiagnosticCode(payload["code"]),
            exception_type=DiagnosticException(payload["exception_type"]),
        )
    except (ValueError, TypeError):
        return None


async def drain_diagnostic(stream: asyncio.StreamReader | None) -> ProcessDiagnostic:
    """Всегда осушать pipe, разбирая лишь первые ограниченные байты и строки."""
    diagnostic = ProcessDiagnostic()
    if stream is None:
        return diagnostic
    total = 0
    pending = bytearray()
    oversized = False
    while True:
        chunk = await stream.read(DiagnosticLimit.CHUNK_BYTES)
        if not chunk:
            break
        remaining = max(0, DiagnosticLimit.BYTES - total)
        total += len(chunk)
        for piece in chunk[:remaining].splitlines(keepends=True):
            ended = piece.endswith(b"\n")
            if len(pending) + len(piece) > DiagnosticLimit.LINE_BYTES:
                pending.clear()
                oversized = True
            elif not oversized:
                pending.extend(piece)
            if ended:
                parsed = None if oversized else parse_diagnostic(bytes(pending).rstrip(b"\r\n"))
                if parsed is not None:
                    diagnostic = parsed
                pending.clear()
                oversized = False
    if pending and not oversized:
        parsed = parse_diagnostic(bytes(pending))
        if parsed is not None:
            diagnostic = parsed
    return diagnostic
