"""Санитизированная классификация ошибок изолированной ML операции."""

import sys
from collections.abc import Callable
from enum import IntEnum, StrEnum

from memes_worker.application.dto.runtime import (
    WorkerExecutionError,
    WorkerFailure,
    WorkerTransportError,
)
from memes_worker.infrastructure.ml.diagnostics import (
    DiagnosticCode,
    DiagnosticException,
    PluginInputError,
    PluginModelConfigurationError,
    ProcessDiagnostic,
    diagnostic_line,
)


class PluginExit(IntEnum):
    """Машинный контракт завершения известного адаптера."""

    INVALID_INPUT = 64
    TEMPORARY = 75
    RESOURCE_EXHAUSTED = 137


class ResourceMessage(StrEnum):
    """Известные признаки нехватки памяти в ML runtime."""

    MEMORY = "out of memory"
    CUDA = "cuda_error_out_of_memory"


def fail(code: PluginExit, diagnostic: ProcessDiagnostic) -> None:
    """Записать только enum-диагностику; детали ошибки не покидают процесс."""
    sys.stderr.write(diagnostic_line(diagnostic) + "\n")
    sys.exit(code)


def entrypoint(action: Callable[[], None]) -> None:
    """Отделить собственную валидацию входа от ошибок SDK и runtime."""
    try:
        action()
    except PluginInputError:
        fail(
            PluginExit.INVALID_INPUT,
            ProcessDiagnostic(DiagnosticCode.INPUT, DiagnosticException.INPUT),
        )
    except PluginModelConfigurationError:
        fail(
            PluginExit.TEMPORARY,
            ProcessDiagnostic(DiagnosticCode.CONFIGURATION, DiagnosticException.CONFIGURATION),
        )
    except WorkerExecutionError as error:
        code = (
            PluginExit.RESOURCE_EXHAUSTED
            if error.reason is WorkerFailure.RESOURCE_EXHAUSTED
            else PluginExit.INVALID_INPUT
            if error.reason is WorkerFailure.INVALID_INPUT
            else PluginExit.TEMPORARY
        )
        diagnostic = (
            DiagnosticCode.RESOURCE
            if code is PluginExit.RESOURCE_EXHAUSTED
            else DiagnosticCode.INPUT
            if code is PluginExit.INVALID_INPUT
            else DiagnosticCode.RUNTIME
        )
        fail(code, ProcessDiagnostic(diagnostic, DiagnosticException.WORKER))
    except WorkerTransportError:
        fail(
            PluginExit.TEMPORARY,
            ProcessDiagnostic(DiagnosticCode.RUNTIME, DiagnosticException.TRANSPORT),
        )
    except MemoryError:
        fail(
            PluginExit.RESOURCE_EXHAUSTED,
            ProcessDiagnostic(DiagnosticCode.RESOURCE, DiagnosticException.MEMORY),
        )
    except RuntimeError as error:
        reason = str(error).lower()
        resource = ResourceMessage.MEMORY in reason or ResourceMessage.CUDA in reason
        fail(
            PluginExit.RESOURCE_EXHAUSTED if resource else PluginExit.TEMPORARY,
            ProcessDiagnostic(
                DiagnosticCode.RESOURCE if resource else DiagnosticCode.RUNTIME,
                DiagnosticException.RUNTIME,
            ),
        )
    except (ValueError, TypeError, OSError, AssertionError, ImportError) as error:
        exception = (
            DiagnosticException.VALUE
            if isinstance(error, ValueError)
            else DiagnosticException.TYPE
            if isinstance(error, TypeError)
            else DiagnosticException.OS
            if isinstance(error, OSError)
            else DiagnosticException.ASSERTION
            if isinstance(error, AssertionError)
            else DiagnosticException.IMPORT
        )
        fail(PluginExit.TEMPORARY, ProcessDiagnostic(DiagnosticCode.RUNTIME, exception))
