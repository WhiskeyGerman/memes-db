"""Публикация и проверяемые характеристики медиа."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import IntEnum, StrEnum
from fractions import Fraction
from typing import NewType, TypeAlias
from uuid import UUID

from memes_kernel.entity import Entity
from memes_kernel.errors import DomainError
from memes_kernel.values import FileRef, Number, require_aware, require_identifier, require_ordered

MediaId = NewType("MediaId", UUID)
AuthorId = NewType("AuthorId", UUID)


class MediaKind(StrEnum):
    """Вид опубликованного содержимого."""

    IMAGE = "image"
    ANIMATION = "animation"
    VIDEO = "video"
    AUDIO = "audio"


class MediaFormat(StrEnum):
    """Допустимый контейнер или растровый формат."""

    JPEG = "jpeg"
    PNG = "png"
    WEBP = "webp"
    AVIF = "avif"
    GIF = "gif"
    APNG = "apng"
    MP4 = "mp4"
    WEBM = "webm"
    MP3 = "mp3"
    M4A = "m4a"
    WAV = "wav"
    OGG = "ogg"
    FLAC = "flac"


class VideoCodec(StrEnum):
    """Разрешённые профили кодирования видео."""

    H264_BASELINE = "h264_baseline"
    H264_MAIN = "h264_main"
    H264_HIGH = "h264_high"
    VP8 = "vp8"
    VP9_PROFILE_ZERO = "vp9_profile_zero"


class AudioCodec(StrEnum):
    """Разрешённые кодеки аудио."""

    MP3 = "mp3"
    AAC_LC = "aac_lc"
    PCM = "pcm"
    OPUS = "opus"
    VORBIS = "vorbis"
    FLAC = "flac"


class MediaState(StrEnum):
    """Состояние публичной публикации."""

    PUBLISHED = "published"
    DELETED = "deleted"


class MediaLimit(IntEnum):
    """Начальные значения настраиваемых продуктовых ограничений."""

    MAXIMUM_BYTES = 104_857_600
    MAXIMUM_SECONDS = 300
    MAXIMUM_IMAGE_SIDE = 16_384
    MAXIMUM_IMAGE_PIXELS = 40_000_000
    VIDEO_LONG_SIDE = 3_840
    VIDEO_SHORT_SIDE = 2_160
    ANIMATION_LONG_SIDE = 1_920
    ANIMATION_SHORT_SIDE = 1_080
    MAXIMUM_FRAMES = 18_000
    MAXIMUM_FPS = 60
    MINIMUM_SAMPLE_RATE = 8_000
    MAXIMUM_SAMPLE_RATE = 48_000
    MAXIMUM_CHANNELS = 2
    PCM_SMALL_DEPTH = 16
    PCM_LARGE_DEPTH = 24
    H264_MAXIMUM_LEVEL_TENTHS = 52
    LEVEL_SCALE = 10


class MediaViolation(StrEnum):
    """Нарушения правил медиа."""

    INVALID_DIMENSIONS = "invalid_dimensions"
    INVALID_DURATION = "invalid_duration"
    INVALID_FRAMES = "invalid_frames"
    INVALID_AUDIO = "invalid_audio"
    INVALID_VIDEO_LEVEL = "invalid_video_level"
    INVALID_STATE = "invalid_media_state"
    UNSUPPORTED_FORMAT = "unsupported_media_format"
    SIZE_LIMIT = "media_size_limit"
    DURATION_LIMIT = "media_duration_limit"
    RESOLUTION_LIMIT = "media_resolution_limit"
    FRAME_LIMIT = "media_frame_limit"
    AUDIO_LIMIT = "media_audio_limit"


@dataclass(frozen=True, slots=True, kw_only=True)
class Dimensions:
    """Размер растра в пикселях."""

    width: int
    height: int

    def __post_init__(self) -> None:
        """Проверить положительность размеров."""
        if min(self.width, self.height) < Number.ONE:
            raise DomainError(MediaViolation.INVALID_DIMENSIONS)

    @property
    def pixels(self) -> int:
        """Получить количество пикселей."""
        return self.width * self.height

    def fits(self, long_side: int, short_side: int) -> bool:
        """Проверить границы с учётом поворота.

        Args:
            long_side: Максимальная длинная сторона.
            short_side: Максимальная короткая сторона.
        """
        return (
            max(self.width, self.height) <= long_side and min(self.width, self.height) <= short_side
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ImageMetadata:
    """Характеристики статического изображения."""

    dimensions: Dimensions


@dataclass(frozen=True, slots=True, kw_only=True)
class AnimationMetadata:
    """Характеристики одного цикла анимации."""

    dimensions: Dimensions
    duration: timedelta
    frames: int

    def __post_init__(self) -> None:
        """Проверить длительность и число кадров."""
        if self.duration <= timedelta():
            raise DomainError(MediaViolation.INVALID_DURATION)
        if self.frames < Number.ONE:
            raise DomainError(MediaViolation.INVALID_FRAMES)


@dataclass(frozen=True, slots=True, kw_only=True)
class AudioMetadata:
    """Характеристики аудио или дорожки."""

    codec: AudioCodec
    duration: timedelta
    channels: int
    sample_rate_hz: int
    bit_depth: int | None = None

    def __post_init__(self) -> None:
        """Проверить структурную допустимость дорожки."""
        if self.duration <= timedelta():
            raise DomainError(MediaViolation.INVALID_DURATION)
        if min(self.channels, self.sample_rate_hz) < Number.ONE:
            raise DomainError(MediaViolation.INVALID_AUDIO)
        depth_codecs = {AudioCodec.PCM, AudioCodec.FLAC}
        allowed_depths = {MediaLimit.PCM_SMALL_DEPTH, MediaLimit.PCM_LARGE_DEPTH}
        if self.codec in depth_codecs and self.bit_depth not in allowed_depths:
            raise DomainError(MediaViolation.INVALID_AUDIO)
        if self.codec not in depth_codecs and self.bit_depth is not None:
            raise DomainError(MediaViolation.INVALID_AUDIO)


@dataclass(frozen=True, slots=True, kw_only=True)
class VideoMetadata:
    """Характеристики видео и необязательной звуковой дорожки."""

    dimensions: Dimensions
    codec: VideoCodec
    duration: timedelta
    frames: int
    frame_rate: Fraction
    audio: AudioMetadata | None = None
    level: Fraction | None = None

    def __post_init__(self) -> None:
        """Проверить структурную допустимость видео."""
        if self.duration <= timedelta():
            raise DomainError(MediaViolation.INVALID_DURATION)
        if self.frames < Number.ONE or self.frame_rate <= Number.ZERO:
            raise DomainError(MediaViolation.INVALID_FRAMES)
        h264 = {VideoCodec.H264_BASELINE, VideoCodec.H264_MAIN, VideoCodec.H264_HIGH}
        if self.codec in h264:
            if self.level is None or not Number.ZERO < self.level <= Fraction(
                MediaLimit.H264_MAXIMUM_LEVEL_TENTHS, MediaLimit.LEVEL_SCALE
            ):
                raise DomainError(MediaViolation.INVALID_VIDEO_LEVEL)
        elif self.level is not None:
            raise DomainError(MediaViolation.INVALID_VIDEO_LEVEL)


MediaMetadata: TypeAlias = ImageMetadata | AnimationMetadata | VideoMetadata | AudioMetadata


@dataclass(frozen=True, slots=True, kw_only=True)
class OriginalMedia:
    """Неизменяемый оригинал с проверенным сочетанием формата и кодека."""

    file: FileRef
    format: MediaFormat
    metadata: MediaMetadata

    def __post_init__(self) -> None:
        """Проверить сочетание контейнера и содержимого."""
        valid = False
        if isinstance(self.metadata, ImageMetadata):
            valid = self.format in {
                MediaFormat.JPEG,
                MediaFormat.PNG,
                MediaFormat.WEBP,
                MediaFormat.AVIF,
                MediaFormat.GIF,
            }
        elif isinstance(self.metadata, AnimationMetadata):
            valid = self.format in {
                MediaFormat.GIF,
                MediaFormat.WEBP,
                MediaFormat.PNG,
                MediaFormat.APNG,
            }
        elif isinstance(self.metadata, AudioMetadata):
            allowed = {
                MediaFormat.MP3: {AudioCodec.MP3},
                MediaFormat.M4A: {AudioCodec.AAC_LC},
                MediaFormat.WAV: {AudioCodec.PCM},
                MediaFormat.OGG: {AudioCodec.OPUS, AudioCodec.VORBIS},
                MediaFormat.FLAC: {AudioCodec.FLAC},
            }
            valid = self.metadata.codec in allowed.get(self.format, set())
        elif isinstance(self.metadata, VideoMetadata):
            video = {
                MediaFormat.MP4: {
                    VideoCodec.H264_BASELINE,
                    VideoCodec.H264_MAIN,
                    VideoCodec.H264_HIGH,
                },
                MediaFormat.WEBM: {VideoCodec.VP8, VideoCodec.VP9_PROFILE_ZERO},
            }
            audio = {
                MediaFormat.MP4: {AudioCodec.AAC_LC},
                MediaFormat.WEBM: {AudioCodec.OPUS, AudioCodec.VORBIS},
            }
            valid = self.metadata.codec in video.get(self.format, set()) and (
                self.metadata.audio is None
                or self.metadata.audio.codec in audio.get(self.format, set())
            )
        if not valid:
            raise DomainError(MediaViolation.UNSUPPORTED_FORMAT)

    @property
    def kind(self) -> MediaKind:
        """Получить вид медиа из фактических характеристик."""
        if isinstance(self.metadata, ImageMetadata):
            return MediaKind.IMAGE
        if isinstance(self.metadata, AnimationMetadata):
            return MediaKind.ANIMATION
        if isinstance(self.metadata, VideoMetadata):
            return MediaKind.VIDEO
        return MediaKind.AUDIO


@dataclass(frozen=True, slots=True, kw_only=True)
class MediaPolicy:
    """Настраиваемые продуктовые пределы приёмки оригинала."""

    maximum_bytes: int = MediaLimit.MAXIMUM_BYTES
    maximum_duration: timedelta = timedelta(seconds=MediaLimit.MAXIMUM_SECONDS)
    image_side: int = MediaLimit.MAXIMUM_IMAGE_SIDE
    image_pixels: int = MediaLimit.MAXIMUM_IMAGE_PIXELS
    video_long_side: int = MediaLimit.VIDEO_LONG_SIDE
    video_short_side: int = MediaLimit.VIDEO_SHORT_SIDE
    animation_long_side: int = MediaLimit.ANIMATION_LONG_SIDE
    animation_short_side: int = MediaLimit.ANIMATION_SHORT_SIDE
    maximum_frames: int = MediaLimit.MAXIMUM_FRAMES
    maximum_fps: int = MediaLimit.MAXIMUM_FPS
    minimum_sample_rate: int = MediaLimit.MINIMUM_SAMPLE_RATE
    maximum_sample_rate: int = MediaLimit.MAXIMUM_SAMPLE_RATE
    maximum_channels: int = MediaLimit.MAXIMUM_CHANNELS

    def __post_init__(self) -> None:
        """Проверить допустимость конфигурации."""
        if (
            min(
                self.maximum_bytes,
                self.image_side,
                self.image_pixels,
                self.video_long_side,
                self.video_short_side,
                self.animation_long_side,
                self.animation_short_side,
                self.maximum_frames,
                self.maximum_fps,
                self.minimum_sample_rate,
                self.maximum_sample_rate,
                self.maximum_channels,
            )
            < Number.ONE
            or self.maximum_duration <= timedelta()
        ):
            raise DomainError(MediaViolation.INVALID_STATE)
        if self.minimum_sample_rate > self.maximum_sample_rate:
            raise DomainError(MediaViolation.INVALID_AUDIO)
        if self.video_long_side < self.video_short_side:
            raise DomainError(MediaViolation.INVALID_DIMENSIONS)
        if self.animation_long_side < self.animation_short_side:
            raise DomainError(MediaViolation.INVALID_DIMENSIONS)

    def validate(self, original: OriginalMedia) -> None:
        """Проверить оригинал перед публикацией.

        Args:
            original: Проверяемый оригинал.

        Raises:
            DomainError: Нарушен продуктовый предел.
        """
        if original.file.size_bytes > self.maximum_bytes:
            raise DomainError(MediaViolation.SIZE_LIMIT)
        metadata = original.metadata
        if isinstance(metadata, ImageMetadata):
            if max(metadata.dimensions.width, metadata.dimensions.height) > self.image_side:
                raise DomainError(MediaViolation.RESOLUTION_LIMIT)
            if metadata.dimensions.pixels > self.image_pixels:
                raise DomainError(MediaViolation.RESOLUTION_LIMIT)
            return
        if metadata.duration > self.maximum_duration:
            raise DomainError(MediaViolation.DURATION_LIMIT)
        if isinstance(metadata, AnimationMetadata):
            if not metadata.dimensions.fits(self.animation_long_side, self.animation_short_side):
                raise DomainError(MediaViolation.RESOLUTION_LIMIT)
            if metadata.frames > self.maximum_frames:
                raise DomainError(MediaViolation.FRAME_LIMIT)
        elif isinstance(metadata, VideoMetadata):
            if not metadata.dimensions.fits(self.video_long_side, self.video_short_side):
                raise DomainError(MediaViolation.RESOLUTION_LIMIT)
            if metadata.frames > self.maximum_frames or metadata.frame_rate > self.maximum_fps:
                raise DomainError(MediaViolation.FRAME_LIMIT)
            if metadata.audio is not None:
                self._validate_audio(metadata.audio)
        else:
            self._validate_audio(metadata)

    def _validate_audio(self, audio: AudioMetadata) -> None:
        """Проверить ограничения звуковой дорожки."""
        if (
            audio.channels > self.maximum_channels
            or not self.minimum_sample_rate <= audio.sample_rate_hz <= self.maximum_sample_rate
        ):
            raise DomainError(MediaViolation.AUDIO_LIMIT)
        if audio.duration > self.maximum_duration:
            raise DomainError(MediaViolation.DURATION_LIMIT)


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class Media(Entity[MediaId]):
    """Публичная публикация уникального оригинала."""

    author_id: AuthorId
    original: OriginalMedia
    published_at: datetime
    state: MediaState = MediaState.PUBLISHED
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        """Проверить согласованность состояния публикации."""
        Entity.__post_init__(self)
        require_identifier(self.author_id)
        require_aware(self.published_at)
        if (self.state is MediaState.DELETED) != (self.deleted_at is not None):
            raise DomainError(MediaViolation.INVALID_STATE)
        if self.deleted_at is not None:
            require_ordered(self.deleted_at, self.published_at)

    @classmethod
    def publish(
        cls,
        *,
        media_id: MediaId,
        author_id: AuthorId,
        original: OriginalMedia,
        policy: MediaPolicy,
        now: datetime,
    ) -> "Media":
        """Принять публикацию по действующей политике.

        Args:
            media_id: Новый идентификатор публикации.
            author_id: Идентификатор загрузившего пользователя.
            original: Оригинальный файл.
            policy: Действующие ограничения.
            now: Серверное время.

        Returns:
            Media: Публичное медиа.
        """
        policy.validate(original)
        return cls(id=media_id, author_id=author_id, original=original, published_at=now)

    def delete(self, *, now: datetime) -> "Media":
        """Логически удалить только эту публикацию.

        Args:
            now: Серверное время удаления.

        Returns:
            Media: Удалённое состояние либо прежнее при повторе.
        """
        if self.state is MediaState.DELETED:
            return self
        require_ordered(now, self.published_at)
        return replace(
            self, state=MediaState.DELETED, deleted_at=now, revision=self.revision.next()
        )
