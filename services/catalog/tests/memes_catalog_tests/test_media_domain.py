from dataclasses import replace
from datetime import datetime, timedelta
from fractions import Fraction
from uuid import uuid4

import pytest

from memes_catalog.domain.media import (
    AnimationMetadata,
    AudioCodec,
    AudioMetadata,
    Dimensions,
    ImageMetadata,
    Media,
    MediaFormat,
    MediaId,
    MediaLimit,
    MediaPolicy,
    MediaState,
    MediaViolation,
    OriginalMedia,
    VideoCodec,
    VideoMetadata,
)
from memes_kernel.errors import DomainError
from memes_kernel.values import FileRef, Number


def test_media_is_public_without_processing(media: Media) -> None:
    assert media.state is MediaState.PUBLISHED


def test_size_boundary_is_inclusive(image: OriginalMedia) -> None:
    policy = MediaPolicy()
    policy.validate(replace(image, file=replace(image.file, size_bytes=MediaLimit.MAXIMUM_BYTES)))
    with pytest.raises(DomainError) as error:
        policy.validate(
            replace(
                image, file=replace(image.file, size_bytes=MediaLimit.MAXIMUM_BYTES + Number.ONE)
            )
        )
    assert error.value.code is MediaViolation.SIZE_LIMIT


def test_animated_webp_has_duration_and_resolution_limits(original_file: FileRef) -> None:
    animation = OriginalMedia(
        file=original_file,
        format=MediaFormat.WEBP,
        metadata=AnimationMetadata(
            dimensions=Dimensions(
                width=MediaLimit.ANIMATION_SHORT_SIDE, height=MediaLimit.ANIMATION_LONG_SIDE
            ),
            duration=timedelta(seconds=MediaLimit.MAXIMUM_SECONDS),
            frames=MediaLimit.MAXIMUM_FRAMES,
        ),
    )
    MediaPolicy().validate(animation)
    assert isinstance(animation.metadata, AnimationMetadata)
    with pytest.raises(DomainError) as error:
        MediaPolicy().validate(
            replace(
                animation,
                metadata=replace(
                    animation.metadata,
                    duration=timedelta(seconds=MediaLimit.MAXIMUM_SECONDS + Number.ONE),
                ),
            )
        )
    assert error.value.code is MediaViolation.DURATION_LIMIT


def test_container_codec_mismatch_is_rejected(original_file: FileRef) -> None:
    with pytest.raises(DomainError) as error:
        OriginalMedia(
            file=original_file,
            format=MediaFormat.MP4,
            metadata=VideoMetadata(
                dimensions=Dimensions(width=640, height=480),
                codec=VideoCodec.VP8,
                duration=timedelta(seconds=Number.ONE),
                frames=Number.ONE,
                frame_rate=Fraction(Number.ONE),
            ),
        )
    assert error.value.code is MediaViolation.UNSUPPORTED_FORMAT


def test_audio_rate_limits_and_lossless_depth(original_file: FileRef) -> None:
    metadata = AudioMetadata(
        codec=AudioCodec.FLAC,
        duration=timedelta(seconds=Number.ONE),
        channels=Number.ONE,
        sample_rate_hz=MediaLimit.MAXIMUM_SAMPLE_RATE,
        bit_depth=MediaLimit.PCM_LARGE_DEPTH,
    )
    original = OriginalMedia(file=original_file, format=MediaFormat.FLAC, metadata=metadata)
    MediaPolicy().validate(original)
    with pytest.raises(DomainError) as error:
        MediaPolicy().validate(
            replace(
                original,
                metadata=replace(
                    metadata, sample_rate_hz=MediaLimit.MAXIMUM_SAMPLE_RATE + Number.ONE
                ),
            )
        )
    assert error.value.code is MediaViolation.AUDIO_LIMIT


def test_image_pixel_budget_is_independent_of_side_budget(image: OriginalMedia) -> None:
    assert isinstance(image.metadata, ImageMetadata)
    oversized = replace(
        image, metadata=replace(image.metadata, dimensions=Dimensions(width=8000, height=8000))
    )
    with pytest.raises(DomainError) as error:
        MediaPolicy().validate(oversized)
    assert error.value.code is MediaViolation.RESOLUTION_LIMIT


def test_delete_targets_id_and_allows_new_identity_for_same_bytes(
    media: Media, now: datetime
) -> None:
    deleted = media.delete(now=now)
    reuploaded = replace(media, id=MediaId(uuid4()))
    assert deleted.state is MediaState.DELETED
    assert deleted.delete(now=now) is deleted
    assert reuploaded.state is MediaState.PUBLISHED
    assert reuploaded.original.file.sha256 == deleted.original.file.sha256
    assert reuploaded.id != deleted.id
