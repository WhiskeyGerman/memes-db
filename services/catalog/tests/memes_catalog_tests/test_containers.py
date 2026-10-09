from collections.abc import Callable
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from memes_catalog.domain.media import AnimationMetadata, MediaFormat, MediaPolicy
from memes_catalog.infrastructure.media.containers import ContainerKind, identify_container
from memes_catalog.infrastructure.media.decoder import MediaDecoder
from memes_kernel.values import FileRef, Sha256


@pytest.fixture
def container_file(tmp_path: Path) -> Callable[[bytes], Path]:
    def create(body: bytes) -> Path:
        path = tmp_path / "arbitrary-extension"
        path.write_bytes(body)
        return path

    return create


@pytest.mark.parametrize(
    "brand,expected",
    [
        (b"isom", ContainerKind.MP4),
        (b"mp42", ContainerKind.MP4),
        (b"M4A ", ContainerKind.MP4),
        (b"qt  ", ContainerKind.OTHER),
        (b"3gp6", ContainerKind.OTHER),
    ],
)
def test_bmff_brand_excludes_mov_and_3gp(
    container_file: Callable[[bytes], Path],
    brand: bytes,
    expected: ContainerKind,
) -> None:
    assert (
        identify_container(container_file(b"\x00\x00\x00\x10ftyp" + brand + b"\0" * 4)) is expected
    )


@pytest.mark.parametrize(
    "doctype,expected", [(b"webm", ContainerKind.WEBM), (b"matroska", ContainerKind.OTHER)]
)
def test_webm_requires_real_ebml_doctype(
    container_file: Callable[[bytes], Path],
    doctype: bytes,
    expected: ContainerKind,
) -> None:
    child = b"\x42\x82" + bytes([128 + len(doctype)]) + doctype
    ebml = b"\x1a\x45\xdf\xa3" + bytes([128 + len(child)]) + child
    assert identify_container(container_file(ebml)) is expected


@pytest.fixture
def poster_apng(tmp_path: Path) -> Path:
    path = tmp_path / "poster.png"
    poster = Image.new("RGB", (16, 16), "black")
    first = Image.new("RGB", (16, 16), "red")
    second = Image.new("RGB", (16, 16), "blue")
    poster.save(
        path,
        default_image=True,
        save_all=True,
        append_images=[first, second],
        duration=[120, 230],
        loop=0,
        format="PNG",
    )
    return path


def test_apng_poster_does_not_count_as_animation_frame(poster_apng: Path) -> None:
    body = poster_apng.read_bytes()
    inspected = MediaDecoder(MediaPolicy(), "ffprobe")._inspect(
        poster_apng,
        FileRef(id=uuid4(), sha256=Sha256(value=sha256(body).hexdigest()), size_bytes=len(body)),
        "snapshot",
    )
    assert inspected.original.format is MediaFormat.APNG
    metadata = inspected.original.metadata
    assert isinstance(metadata, AnimationMetadata)
    assert metadata.frames == 2
    assert metadata.duration == timedelta(milliseconds=350)
