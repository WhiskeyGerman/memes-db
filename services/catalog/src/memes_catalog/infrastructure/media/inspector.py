"""Ограниченная передача и изолированный процесс проверки медиа."""

import asyncio
import json
import os
import signal
import sys
from dataclasses import replace
from enum import IntEnum, StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from uuid import UUID

from memes_catalog.application.dto.catalog import InspectedMedia
from memes_catalog.application.ports.resources import IMediaInspector
from memes_catalog.domain.media import MediaFormat, MediaKind, MediaPolicy
from memes_catalog.infrastructure.persistence.mapping import original
from memes_catalog.infrastructure.storage.s3 import S3ObjectStorage
from memes_platform.application.dto.json import JsonObject
from memes_platform.application.errors import ApplicationError, ErrorCode


class InspectionLimit(IntEnum):
    """Обязательные пределы дочернего декодера."""

    CONCURRENCY = 4
    SECONDS = 90
    CPU_SECONDS = 60
    MEMORY_BYTES = 1610612736
    OUTPUT_BYTES = 2097152
    STOP_SECONDS = 5


class InspectionSetting(StrEnum):
    """Разрешённый процесс и параметры окружения."""

    MODULE = "memes_catalog.infrastructure.media.inspect_command"
    POSIX = "posix"
    PATH = "PATH"
    SYSTEM_ROOT = "SystemRoot"
    TMP = "TMP"
    TEMP = "TEMP"


class MediaInspector(IMediaInspector):
    """Проверить содержимое вне event loop и памяти HTTP-процесса."""

    def __init__(
        self,
        storage: S3ObjectStorage,
        policy: MediaPolicy,
        *,
        ffprobe: str = "ffprobe",
        concurrency: int = InspectionLimit.CONCURRENCY,
        timeout: int = InspectionLimit.SECONDS,
    ) -> None:
        """Сохранить реальные ресурсы и ограничить параллельное декодирование.

        Args:
            storage: Приватное хранилище.
            policy: Правила приёмки.
            ffprobe: Путь декодера.
            concurrency: Параллельные временные копии.
            timeout: Максимальное время дочернего процесса.
        """
        self.storage = storage
        self.policy = policy
        self.ffprobe = ffprobe
        self.semaphore = asyncio.Semaphore(concurrency)
        self.timeout = timeout

    async def inspect_object(
        self, object_key: str, *, file_id: UUID, maximum_bytes: int
    ) -> InspectedMedia:
        """Проверить неизменяемый GET snapshot с bounded lifetime."""
        async with self.semaphore:
            with TemporaryDirectory(prefix="memes-inspection-") as directory:
                path = Path(directory) / "source"
                file, etag = await self.storage.download_verified(
                    object_key, path, maximum_bytes=maximum_bytes
                )
                file = replace(file, id=file_id)
                output_path, error_path = Path(directory) / "result", Path(directory) / "error"
                policy = {
                    "maximum_bytes": self.policy.maximum_bytes,
                    "maximum_seconds": int(self.policy.maximum_duration.total_seconds()),
                }
                command = [
                    sys.executable,
                    "-m",
                    InspectionSetting.MODULE,
                    str(path),
                    str(file.id),
                    file.sha256.value,
                    str(file.size_bytes),
                    self.ffprobe,
                    json.dumps(policy),
                ]
                if os.name == InspectionSetting.POSIX:
                    command = [
                        "prlimit",
                        f"--as={InspectionLimit.MEMORY_BYTES}",
                        f"--cpu={InspectionLimit.CPU_SECONDS}",
                        f"--fsize={InspectionLimit.OUTPUT_BYTES}",
                        "--",
                        *command,
                    ]
                environment: dict[str, str] = {
                    name.value: os.environ[name]
                    for name in InspectionSetting
                    if name in os.environ
                    and name not in (InspectionSetting.MODULE, InspectionSetting.POSIX)
                }
                process: asyncio.subprocess.Process | None = None
                try:
                    with output_path.open("wb") as output, error_path.open("wb") as error:
                        process = await asyncio.create_subprocess_exec(
                            *command,
                            stdout=output,
                            stderr=error,
                            env=environment,
                            start_new_session=os.name == InspectionSetting.POSIX,
                        )
                        await asyncio.wait_for(process.wait(), timeout=self.timeout)
                    if (
                        process.returncode != 0
                        or output_path.stat().st_size > InspectionLimit.OUTPUT_BYTES
                    ):
                        raise ApplicationError(
                            ErrorCode.INVALID_INPUT, "Медиа не прошло проверку декодера."
                        )
                    payload = cast(JsonObject, json.loads(output_path.read_bytes()))
                    media_format = MediaFormat(str(payload["format"]))
                    data = cast(JsonObject, payload["metadata"])
                    decoded = original(
                        file=file,
                        media_format=media_format,
                        kind=MediaKind(str(payload["kind"])),
                        data=data,
                    )
                    self.policy.validate(decoded)
                    return InspectedMedia(original=decoded, metadata=data, etag=etag)
                except TimeoutError as error:
                    raise ApplicationError(
                        ErrorCode.INVALID_INPUT, "Превышено время проверки медиа."
                    ) from error
                except (OSError, ValueError, KeyError, TypeError) as error:
                    if isinstance(error, ApplicationError):
                        raise
                    raise ApplicationError(ErrorCode.INVALID_INPUT) from error
                finally:
                    if process is not None:
                        await stop_inspection(process)


def signal_inspection(process: asyncio.subprocess.Process, *, force: bool) -> None:
    """Остановить только собственное дерево изолированного декодера.

    Args:
        process: Процесс, созданный в отдельной POSIX session.
        force: Требуется принудительное завершение после истечения grace.
    """
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


async def stop_inspection(process: asyncio.subprocess.Process) -> None:
    """Завершить декодер и оставшиеся дочерние процессы перед удалением файлов.

    Args:
        process: Собственный изолированный процесс проверки.
    """
    signal_inspection(process, force=False)
    try:
        await asyncio.wait_for(process.wait(), timeout=InspectionLimit.STOP_SECONDS)
    except TimeoutError:
        signal_inspection(process, force=True)
        await process.wait()
    finally:
        # ffprobe может пережить аварийно завершившегося родителя.
        signal_inspection(process, force=True)
