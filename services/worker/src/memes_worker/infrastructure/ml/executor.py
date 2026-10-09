"""Настоящая предобработка ffmpeg и изолированные ML-процессы."""

import asyncio
import json
import sys
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from errno import ENOMEM, ENOSPC
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx

from memes_worker.application.dto.runtime import (
    JSONValue,
    Payload,
    WorkAssignment,
    WorkerExecutionError,
    WorkerFailure,
    WorkResult,
)
from memes_worker.application.execution import Operation
from memes_worker.application.ports.execution import ITaskExecutor
from memes_worker.infrastructure.config.settings import WorkerDefault, WorkerSettings
from memes_worker.infrastructure.ml.diagnostics import ProcessProgram
from memes_worker.infrastructure.ml.process import execute
from memes_worker.infrastructure.wire import object_payload, value_string


class DecoderLimit(IntEnum):
    """Бюджеты одного источника."""

    DURATION_SECONDS = 300
    PREVIEW_SIDE = 512
    AUDIO_RATE = 16000
    MILLISECONDS = 1000
    MAX_DISK_BYTES = 536870912
    LAST_FRAME_OFFSET_MS = 1
    ENDPOINT_FRAMES = 2
    FRAMES = 18000


class DecoderName(StrEnum):
    """Известные операции и выходы."""

    INPUT = "source"
    PREVIEW = "preview.jpg"
    FRAMES = "frame-%06d.jpg"
    REQUEST = "request.json"
    RESPONSE = "response.json"
    FFMPEG_LOG = "error"
    OCR_PLUGIN = "memes_worker.infrastructure.ml.ocr"
    ASR_PLUGIN = "memes_worker.infrastructure.ml.asr"
    EMBEDDING_PLUGIN = "memes_worker.infrastructure.ml.embedding"
    IMAGE_PLUGIN = "memes_worker.infrastructure.ml.images"


class MediaExecutor(ITaskExecutor):
    """Декодер получает лишь временную директорию назначенной задачи."""

    def __init__(self, client: httpx.AsyncClient, settings: WorkerSettings) -> None:
        """Принять транспорт файлов и бюджеты устройства."""
        self.client = client
        self.settings = settings
        self.work_directory = settings.cache_directory / "work"
        self.work_directory.mkdir(parents=True, exist_ok=True)

    async def execute(self, work: WorkAssignment) -> WorkResult:
        """Отделить недопустимый источник от локальных сбоев runtime."""
        try:
            return await self._execute(work)
        except WorkerExecutionError:
            raise
        except OSError as error:
            reason = (
                WorkerFailure.RESOURCE_EXHAUSTED
                if error.errno in {ENOMEM, ENOSPC}
                else WorkerFailure.TEMPORARY
            )
            raise WorkerExecutionError(reason) from error
        except (ValueError, TypeError) as error:
            raise WorkerExecutionError(WorkerFailure.TEMPORARY) from error

    async def _execute(self, work: WorkAssignment) -> WorkResult:
        """Сохранить deadline и удалить локальные файлы после одной попытки."""
        with TemporaryDirectory(prefix="task-", dir=self.work_directory) as temporary:
            directory = Path(temporary).resolve()
            source = directory / DecoderName.INPUT
            remaining = min(
                self.settings.process_seconds,
                (work.assignment.deadline - datetime.now(UTC)).total_seconds(),
            )
            if remaining <= 0:
                raise WorkerExecutionError(WorkerFailure.OWNERSHIP_LOST)
            try:
                async with asyncio.timeout(remaining):
                    if (
                        work.query_text is None
                        and work.assignment.key.operation is not Operation.TEXT_EMBEDDING
                    ):
                        await self._download(work, source)
                    result, preview = await self._operation(work, source, directory)
                    return WorkResult(
                        key=work.assignment.key,
                        payload={
                            "kind": work.assignment.key.operation.value,
                            "feature_version": work.feature_version,
                            "result": result,
                        },
                        preview=preview,
                    )
            except TimeoutError as error:
                raise WorkerExecutionError(WorkerFailure.RESOURCE_EXHAUSTED) from error

    async def _download(self, work: WorkAssignment, target: Path) -> None:
        """Проверить фактический размер и SHA-256 потока до декодирования."""
        digest = sha256()
        size = 0
        try:
            async with self.client.stream("GET", work.download_url) as response:
                response.raise_for_status()
                with target.open("wb") as output:
                    async for chunk in response.aiter_bytes(WorkerDefault.CHUNK_BYTES):
                        size += len(chunk)
                        if size > min(
                            work.assignment.source.size_bytes, WorkerDefault.MAX_SOURCE_BYTES
                        ):
                            raise WorkerExecutionError(WorkerFailure.INVALID_INPUT)
                        digest.update(chunk)
                        output.write(chunk)
        except httpx.HTTPError as error:
            raise WorkerExecutionError(WorkerFailure.TEMPORARY) from error
        if (
            size != work.assignment.source.size_bytes
            or digest.hexdigest() != work.assignment.source.sha256.value
        ):
            raise WorkerExecutionError(WorkerFailure.INVALID_INPUT)

    async def _ffmpeg(self, directory: Path, arguments: list[str]) -> bytes:
        """Декодеру запрещены сетевые протоколы и интерактивный ввод."""
        output = await execute(
            [
                self.settings.ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                DecoderName.FFMPEG_LOG,
                "-protocol_whitelist",
                "file,pipe",
                "-y",
                *arguments,
            ],
            timeout_seconds=self.settings.process_seconds,
            directory=directory,
            program=ProcessProgram.FFMPEG,
        )
        if (
            sum(path.stat().st_size for path in directory.iterdir() if path.is_file())
            > DecoderLimit.MAX_DISK_BYTES
        ):
            raise WorkerExecutionError(WorkerFailure.RESOURCE_EXHAUSTED)
        return output

    async def _duration(self, source: Path, directory: Path) -> float:
        """Фактическая длительность не берется на веру из метаданных задания."""
        raw = await execute(
            [
                self.settings.ffprobe,
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                str(source),
            ],
            timeout_seconds=self.settings.process_seconds,
            directory=directory,
            program=ProcessProgram.FFPROBE,
        )
        data = object_payload(json.loads(raw))
        duration = float(value_string(object_payload(data.get("format")).get("duration")))
        if not 0 < duration <= DecoderLimit.DURATION_SECONDS:
            raise WorkerExecutionError(WorkerFailure.INVALID_INPUT)
        return duration

    async def _operation(
        self, work: WorkAssignment, source: Path, directory: Path
    ) -> tuple[Payload, bytes | None]:
        """Пятиминутное медиа разбивается до вызова модели."""
        operation = work.assignment.key.operation
        if operation is Operation.PREVIEW:
            preview_path = directory / DecoderName.PREVIEW
            if work.media_kind in {"image", "animation"}:
                await self._images(source, directory, preview=True)
            else:
                await self._ffmpeg(
                    directory,
                    [
                        "-i",
                        str(source),
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=512:512:force_original_aspect_ratio=decrease",
                        str(preview_path),
                    ],
                )
            preview = preview_path.read_bytes()
            return {
                "file": {
                    "id": str(uuid4()),
                    "sha256": sha256(preview).hexdigest(),
                    "size_bytes": len(preview),
                },
                "object_key": work.preview_key,
            }, preview
        if operation is Operation.TEXT_EMBEDDING:
            fragments: list[JSONValue]
            if work.query_text is not None:
                fragments = [{"text": work.query_text, "span": None}]
            else:
                raw_fragments = work.metadata.get("text_fragments")
                if not isinstance(raw_fragments, list):
                    raise WorkerExecutionError(WorkerFailure.INVALID_INPUT)
                fragments = raw_fragments
            return await self._plugin(
                work,
                directory,
                DecoderName.EMBEDDING_PLUGIN,
                fragments,
            ), None
        if operation is Operation.OCR:
            frames = await self._frames(work, source, directory)
            return await self._plugin(work, directory, DecoderName.OCR_PLUGIN, frames), None
        if operation in {Operation.TRANSCRIPT, Operation.AUDIO_EMBEDDING}:
            audio_probe = await execute(
                [
                    self.settings.ffprobe,
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-select_streams",
                    "a",
                    "-show_entries",
                    "stream=index",
                    "-of",
                    "json",
                    str(source),
                ],
                timeout_seconds=self.settings.process_seconds,
                directory=directory,
                program=ProcessProgram.FFPROBE,
                operation=operation,
            )
            if not object_payload(json.loads(audio_probe)).get("streams"):
                return {"fragments": []} if operation is Operation.TRANSCRIPT else {
                    "items": []
                }, None
            duration = await self._duration(source, directory)
            segments = await self._segments(source, directory, duration=duration, audio=True)
            plugin = (
                DecoderName.ASR_PLUGIN
                if operation is Operation.TRANSCRIPT
                else DecoderName.EMBEDDING_PLUGIN
            )
            return await self._plugin(work, directory, plugin, segments), None
        if work.media_kind in {"image", "animation"}:
            frames = await self._images(source, directory, preview=False)
            if work.media_kind == "animation":
                frames = self._animation_fragments(frames)
            return await self._plugin(work, directory, DecoderName.EMBEDDING_PLUGIN, frames), None
        duration = await self._duration(source, directory)
        segments = await self._segments(source, directory, duration=duration, audio=False)
        return await self._plugin(work, directory, DecoderName.EMBEDDING_PLUGIN, segments), None

    async def _frames(self, work: WorkAssignment, source: Path, directory: Path) -> list[JSONValue]:
        """OCR видит всю временную шкалу, первый и последний кадры."""
        if work.media_kind in {"image", "animation"}:
            return await self._images(source, directory, preview=False)
        duration = await self._duration(source, directory)
        rate = min(
            WorkerDefault.FPS, (WorkerDefault.FRAME_LIMIT - DecoderLimit.ENDPOINT_FRAMES) / duration
        )
        await self._ffmpeg(
            directory,
            [
                "-i",
                str(source),
                "-vf",
                f"fps={rate},scale=512:512:force_original_aspect_ratio=decrease:force_divisible_by=2",
                "-frames:v",
                str(WorkerDefault.FRAME_LIMIT - DecoderLimit.ENDPOINT_FRAMES),
                str(directory / DecoderName.FRAMES),
            ],
        )
        first = directory / "first.jpg"
        await self._ffmpeg(
            directory,
            [
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale=512:512:force_original_aspect_ratio=decrease:force_divisible_by=2",
                str(first),
            ],
        )
        last = directory / "last.jpg"
        await self._ffmpeg(
            directory,
            [
                "-i",
                str(source),
                "-t",
                str(duration),
                "-vf",
                "scale=512:512:force_original_aspect_ratio=decrease:force_divisible_by=2",
                "-frames:v",
                str(DecoderLimit.FRAMES),
                "-update",
                "1",
                str(last),
            ],
        )
        frames = [first, *sorted(directory.glob("frame-*.jpg"))]
        entries: list[JSONValue] = []
        seen: set[str] = set()
        for index, frame in enumerate(frames):
            fingerprint = sha256(frame.read_bytes()).hexdigest()
            if fingerprint not in seen:
                start = min(
                    round(max(0, index - 1) / rate * DecoderLimit.MILLISECONDS),
                    round(duration * DecoderLimit.MILLISECONDS) - 1,
                )
                entries.append(
                    {
                        "path": str(frame),
                        "span": {
                            "start_ms": start,
                            "end_ms": min(
                                round(duration * DecoderLimit.MILLISECONDS),
                                start + round(DecoderLimit.MILLISECONDS / rate),
                            ),
                        },
                    }
                )
                seen.add(fingerprint)
        if last.is_file() and sha256(last.read_bytes()).hexdigest() not in seen:
            entries.append(
                {
                    "path": str(last),
                    "span": {
                        "start_ms": round(duration * DecoderLimit.MILLISECONDS) - 1,
                        "end_ms": round(duration * DecoderLimit.MILLISECONDS),
                    },
                }
            )
        return entries

    async def _segments(
        self, source: Path, directory: Path, *, duration: float, audio: bool
    ) -> list[JSONValue]:
        """Фрагменты имеют фиксированный предел и перекрытие две секунды."""
        length = (
            WorkerDefault.AUDIO_FRAGMENT_SECONDS if audio else WorkerDefault.VIDEO_FRAGMENT_SECONDS
        )
        step = length - WorkerDefault.OVERLAP_SECONDS
        start = 0.0
        segments: list[JSONValue] = []
        while start < duration:
            end = min(duration, start + length)
            destination = directory / f"segment-{len(segments)}.{'wav' if audio else 'mp4'}"
            arguments = ["-ss", str(start), "-i", str(source), "-t", str(end - start)]
            arguments.extend(
                ["-vn", "-ac", "1", "-ar", str(DecoderLimit.AUDIO_RATE), "-c:a", "pcm_s16le"]
                if audio
                else [
                    "-an",
                    "-vf",
                    "scale=512:512:force_original_aspect_ratio=decrease:force_divisible_by=2",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                ]
            )
            await self._ffmpeg(directory, [*arguments, str(destination)])
            segments.append(
                {
                    "path": str(destination),
                    "span": {
                        "start_ms": round(start * DecoderLimit.MILLISECONDS),
                        "end_ms": round(end * DecoderLimit.MILLISECONDS),
                    },
                }
            )
            if end >= duration:
                break
            start += step
        return segments

    async def _images(self, source: Path, directory: Path, *, preview: bool) -> list[JSONValue]:
        """Pillow subprocess корректно обрабатывает GIF/WebP/APNG disposal."""
        request = directory / "image-request.json"
        response = directory / "image-response.json"
        request.write_text(
            json.dumps(
                {
                    "source": str(source),
                    "directory": str(directory),
                    "operation": "preview" if preview else "frames",
                }
            ),
            encoding="utf-8",
        )
        await execute(
            [sys.executable, "-m", DecoderName.IMAGE_PLUGIN, str(request), str(response)],
            timeout_seconds=self.settings.process_seconds,
            directory=directory,
            program=ProcessProgram.IMAGES,
            operation=Operation.PREVIEW if preview else Operation.VISUAL_EMBEDDING,
        )
        data = object_payload(json.loads(response.read_text(encoding="utf-8")))
        if preview:
            return []
        frames = data.get("frames")
        if not isinstance(frames, list):
            raise WorkerExecutionError(WorkerFailure.INVALID_INPUT)
        return frames

    @staticmethod
    def _animation_fragments(frames: list[JSONValue]) -> list[JSONValue]:
        """Передавать модели лишь кадры ограниченного временного фрагмента."""
        fragments: list[JSONValue] = []
        spans = [object_payload(object_payload(frame).get("span")) for frame in frames]
        total = max(int(str(span["end_ms"])) for span in spans)
        start = 0
        length = WorkerDefault.VIDEO_FRAGMENT_SECONDS * DecoderLimit.MILLISECONDS
        step = (
            WorkerDefault.VIDEO_FRAGMENT_SECONDS - WorkerDefault.OVERLAP_SECONDS
        ) * DecoderLimit.MILLISECONDS
        while start < total:
            end = min(total, start + length)
            selected: list[JSONValue] = [
                object_payload(frame)["path"]
                for frame in frames
                if int(str(object_payload(object_payload(frame)["span"])["end_ms"])) > start
                and int(str(object_payload(object_payload(frame)["span"])["start_ms"])) < end
            ]
            if selected:
                fragments.append({"frames": selected, "span": {"start_ms": start, "end_ms": end}})
            if end >= total:
                break
            start += step
        return fragments

    async def _plugin(
        self, work: WorkAssignment, directory: Path, module: DecoderName, inputs: list[JSONValue]
    ) -> Payload:
        """Дочерний процесс загружает только выбранную модель и локальные веса."""
        request: Payload = {
            "inputs": inputs,
            "operation": work.assignment.key.operation.value,
            "source_type": work.source_type,
            "device": self.settings.device,
            "ocr_use_gpu": self.settings.ocr_use_gpu,
            "dimensions": self.settings.embedding_dimensions,
            "jina_model": str(self.settings.jina_model_directory)
            if self.settings.jina_model_directory
            else None,
            "asr_model": str(self.settings.asr_model_directory)
            if self.settings.asr_model_directory
            else None,
            "ocr_detection": str(self.settings.ocr_detection_directory)
            if self.settings.ocr_detection_directory
            else None,
            "ocr_ru": str(self.settings.ocr_ru_directory)
            if self.settings.ocr_ru_directory
            else None,
            "ocr_en": str(self.settings.ocr_en_directory)
            if self.settings.ocr_en_directory
            else None,
        }
        request_path = directory / DecoderName.REQUEST
        response_path = directory / DecoderName.RESPONSE
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        await execute(
            [
                self.settings.ml_python or sys.executable,
                "-m",
                module,
                str(request_path),
                str(response_path),
            ],
            timeout_seconds=self.settings.process_seconds,
            directory=directory,
            program=ProcessProgram(module.value),
            operation=work.assignment.key.operation,
        )
        if response_path.stat().st_size > WorkerDefault.MAX_RESULT_BYTES:
            raise WorkerExecutionError(WorkerFailure.RESOURCE_EXHAUSTED)
        return object_payload(json.loads(response_path.read_text(encoding="utf-8")))
