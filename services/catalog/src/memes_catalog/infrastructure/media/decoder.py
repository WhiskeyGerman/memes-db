"""Фактическая проверка браузерных форматов Pillow и ffprobe."""

import json
import subprocess
from datetime import timedelta
from enum import IntEnum, StrEnum
from fractions import Fraction
from pathlib import Path
from typing import cast

from PIL import Image, UnidentifiedImageError

from memes_catalog.application.dto.catalog import CatalogDefault, InspectedMedia
from memes_catalog.domain.media import (
    AnimationMetadata,
    AudioCodec,
    AudioMetadata,
    Dimensions,
    ImageMetadata,
    MediaFormat,
    MediaLimit,
    MediaPolicy,
    OriginalMedia,
    VideoCodec,
    VideoMetadata,
)
from memes_catalog.infrastructure.media.containers import ContainerKind, identify_container
from memes_catalog.infrastructure.persistence.mapping import MetadataField
from memes_kernel.values import FileRef, Number
from memes_platform.application.dto.json import JsonObject
from memes_platform.application.errors import ApplicationError, ErrorCode


class InspectorLimit(IntEnum):
    """Ресурсные пределы технического декодирования."""

    PROBE_SECONDS = 60
    PROBE_OUTPUT_BYTES = 2097152
    PIXEL_DEPTH = 8
    DEFAULT_FRAME_MS = 100


class ProbeField(StrEnum):
    """Стандартные поля ответа ffprobe."""

    STREAMS = "streams"
    FORMAT = "format"
    FORMAT_NAME = "format_name"
    TYPE = "codec_type"
    AUDIO = "audio"
    VIDEO = "video"
    CODEC = "codec_name"
    PROFILE = "profile"
    DURATION = "duration"
    FRAMES = "nb_read_frames"
    FPS = "avg_frame_rate"
    WIDTH = "width"
    HEIGHT = "height"
    CHANNELS = "channels"
    RATE = "sample_rate"
    DEPTH = "bits_per_raw_sample"
    PIXEL = "pix_fmt"
    LEVEL = "level"


class MediaDecoder:
    """Фактический декодер, запускаемый только в ограниченном дочернем процессе."""

    def __init__(self, policy: MediaPolicy, ffprobe: str) -> None:
        """Сохранить пределы декодирования.

        Args:
            policy: Правила приёмки.
            ffprobe: Разрешённый исполняемый файл.
        """
        self.policy = policy
        self.ffprobe = ffprobe

    def _inspect(self, path: Path, file: FileRef, etag: str) -> InspectedMedia:
        """Определить формат по декодируемому содержимому."""
        try:
            image = Image.open(path)
        except UnidentifiedImageError:
            return self._probe(path, file, etag)
        with image:
            format_name = (image.format or "").lower()
            allowed = {
                media_format.value: media_format
                for media_format in (
                    MediaFormat.JPEG,
                    MediaFormat.PNG,
                    MediaFormat.WEBP,
                    MediaFormat.AVIF,
                    MediaFormat.GIF,
                )
            }
            if format_name not in allowed:
                raise ApplicationError(ErrorCode.INVALID_INPUT)
            dimensions = Dimensions(width=image.width, height=image.height)
            data: JsonObject = {
                MetadataField.DIMENSIONS: {
                    MetadataField.WIDTH: image.width,
                    MetadataField.HEIGHT: image.height,
                }
            }
            first_frame = Number.ONE if image.info.get("default_image", False) else Number.ZERO
            frames = getattr(image, "n_frames", Number.ONE) - first_frame
            if frames > self.policy.maximum_frames:
                raise ApplicationError(ErrorCode.INVALID_INPUT)
            if frames > Number.ONE or first_frame > Number.ZERO:
                if not dimensions.fits(
                    self.policy.animation_long_side, self.policy.animation_short_side
                ):
                    raise ApplicationError(ErrorCode.INVALID_INPUT)
                duration_ms: int = Number.ZERO
                for index in range(first_frame, first_frame + frames):
                    image.seek(index)
                    image.load()
                    duration_ms += max(
                        Number.ONE, int(image.info.get("duration", InspectorLimit.DEFAULT_FRAME_MS))
                    )
                metadata = AnimationMetadata(
                    dimensions=dimensions,
                    duration=timedelta(milliseconds=duration_ms),
                    frames=frames,
                )
                data.update({MetadataField.DURATION: duration_ms, MetadataField.FRAMES: frames})
                original = OriginalMedia(
                    file=file,
                    format=MediaFormat.APNG
                    if format_name == MediaFormat.PNG
                    else allowed[format_name],
                    metadata=metadata,
                )
            else:
                if dimensions.pixels > self.policy.image_pixels:
                    raise ApplicationError(ErrorCode.INVALID_INPUT)
                image.load()
                original = OriginalMedia(
                    file=file,
                    format=allowed[format_name],
                    metadata=ImageMetadata(dimensions=dimensions),
                )
            return InspectedMedia(original=original, metadata=data, etag=etag)

    @staticmethod
    def _audio(stream: JsonObject, duration: Fraction) -> tuple[AudioMetadata, JsonObject]:
        """Проверить разрешённый кодек аудиодорожки."""
        codec_name = str(stream[ProbeField.CODEC])
        codecs = {
            "mp3": AudioCodec.MP3,
            "aac": AudioCodec.AAC_LC,
            "opus": AudioCodec.OPUS,
            "vorbis": AudioCodec.VORBIS,
            "flac": AudioCodec.FLAC,
            "pcm_s16le": AudioCodec.PCM,
            "pcm_s24le": AudioCodec.PCM,
        }
        if codec_name not in codecs or (
            codec_name == "aac" and stream.get(ProbeField.PROFILE) != "LC"
        ):
            raise ApplicationError(ErrorCode.INVALID_INPUT)
        codec = codecs[codec_name]
        depth: int | None = None
        if codec is AudioCodec.PCM:
            depth = (
                MediaLimit.PCM_SMALL_DEPTH
                if codec_name == "pcm_s16le"
                else MediaLimit.PCM_LARGE_DEPTH
            )
        if codec is AudioCodec.FLAC:
            depth = int(str(stream[ProbeField.DEPTH]))
        duration_ms = int(duration * CatalogDefault.MILLISECONDS)
        data: JsonObject = {
            MetadataField.CODEC: codec.value,
            MetadataField.DURATION: duration_ms,
            MetadataField.CHANNELS: int(str(stream[ProbeField.CHANNELS])),
            MetadataField.SAMPLE_RATE: int(str(stream[ProbeField.RATE])),
            MetadataField.BIT_DEPTH: depth,
        }
        return AudioMetadata(
            codec=codec,
            duration=timedelta(milliseconds=duration_ms),
            channels=int(str(stream[ProbeField.CHANNELS])),
            sample_rate_hz=int(str(stream[ProbeField.RATE])),
            bit_depth=depth,
        ), data

    def _probe(self, path: Path, file: FileRef, etag: str) -> InspectedMedia:
        """Проверить контейнер, дорожки и браузерный профиль видео."""
        result = subprocess.run(  # noqa: S603 - фиксированный argv, shell=False, локальный временный файл.
            [
                self.ffprobe,
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-count_frames",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            check=True,
            timeout=InspectorLimit.PROBE_SECONDS,
            shell=False,
        )
        if len(result.stdout) > InspectorLimit.PROBE_OUTPUT_BYTES:
            raise ApplicationError(ErrorCode.INVALID_INPUT)
        payload = cast(JsonObject, json.loads(result.stdout))
        streams = cast(list[JsonObject], payload[ProbeField.STREAMS])
        videos = [stream for stream in streams if stream.get(ProbeField.TYPE) == ProbeField.VIDEO]
        audios = [stream for stream in streams if stream.get(ProbeField.TYPE) == ProbeField.AUDIO]
        if (
            len(videos) > Number.ONE
            or len(audios) > Number.ONE
            or len(streams) != len(videos) + len(audios)
        ):
            raise ApplicationError(ErrorCode.INVALID_INPUT)
        container = cast(JsonObject, payload[ProbeField.FORMAT])
        names = str(container[ProbeField.FORMAT_NAME]).split(",")
        browser_container = identify_container(path)
        duration = Fraction(str(container[ProbeField.DURATION]))
        if not Number.ZERO < duration <= self.policy.maximum_duration.total_seconds():
            raise ApplicationError(ErrorCode.INVALID_INPUT)
        if videos:
            video = videos[Number.ZERO]
            if video.get(ProbeField.PIXEL) not in ("yuv420p", "yuvj420p"):
                raise ApplicationError(ErrorCode.INVALID_INPUT)
            codec_name = str(video[ProbeField.CODEC])
            profiles = {
                "Constrained Baseline": VideoCodec.H264_BASELINE,
                "Baseline": VideoCodec.H264_BASELINE,
                "Main": VideoCodec.H264_MAIN,
                "High": VideoCodec.H264_HIGH,
            }
            level: Fraction | None = None
            if (
                codec_name == "h264"
                and str(video.get(ProbeField.PROFILE)) in profiles
                and "mp4" in names
                and browser_container is ContainerKind.MP4
            ):
                codec = profiles[str(video[ProbeField.PROFILE])]
                media_format = MediaFormat.MP4
                level = Fraction(int(str(video[ProbeField.LEVEL])), MediaLimit.LEVEL_SCALE)
            elif (
                codec_name in ("vp8", "vp9")
                and "webm" in names
                and browser_container is ContainerKind.WEBM
            ):
                if codec_name == "vp9" and video.get(ProbeField.PROFILE) != "Profile 0":
                    raise ApplicationError(ErrorCode.INVALID_INPUT)
                codec = VideoCodec.VP8 if codec_name == "vp8" else VideoCodec.VP9_PROFILE_ZERO
                media_format = MediaFormat.WEBM
            else:
                raise ApplicationError(ErrorCode.INVALID_INPUT)
            audio, audio_data = (
                self._audio(audios[Number.ZERO], duration) if audios else (None, None)
            )
            frames = int(str(video[ProbeField.FRAMES]))
            rate = Fraction(str(video[ProbeField.FPS]))
            dimensions = Dimensions(
                width=int(str(video[ProbeField.WIDTH])), height=int(str(video[ProbeField.HEIGHT]))
            )
            metadata = VideoMetadata(
                dimensions=dimensions,
                codec=codec,
                duration=timedelta(seconds=float(duration)),
                frames=frames,
                frame_rate=rate,
                audio=audio,
                level=level,
            )
            data: JsonObject = {
                MetadataField.DIMENSIONS: {
                    MetadataField.WIDTH: dimensions.width,
                    MetadataField.HEIGHT: dimensions.height,
                },
                MetadataField.CODEC: codec.value,
                MetadataField.DURATION: int(duration * CatalogDefault.MILLISECONDS),
                MetadataField.FRAMES: frames,
                MetadataField.FRAME_RATE: str(rate),
                MetadataField.AUDIO: audio_data,
                MetadataField.LEVEL: None if level is None else str(level),
            }
            original = OriginalMedia(file=file, format=media_format, metadata=metadata)
        elif audios:
            audio, data = self._audio(audios[Number.ZERO], duration)
            formats = {
                "mp3": MediaFormat.MP3,
                "wav": MediaFormat.WAV,
                "ogg": MediaFormat.OGG,
                "flac": MediaFormat.FLAC,
            }
            audio_format = (
                MediaFormat.M4A
                if browser_container is ContainerKind.MP4
                else formats.get(names[Number.ZERO])
            )
            if audio_format is None:
                raise ApplicationError(ErrorCode.INVALID_INPUT)
            original = OriginalMedia(file=file, format=audio_format, metadata=audio)
        else:
            raise ApplicationError(ErrorCode.INVALID_INPUT)
        return InspectedMedia(original=original, metadata=data, etag=etag)
