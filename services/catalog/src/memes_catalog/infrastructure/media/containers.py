"""Определение именно браузерных контейнеров по заголовку файла."""

from enum import IntEnum, StrEnum
from pathlib import Path

from memes_kernel.values import Number


class ContainerKind(StrEnum):
    """Браузерные мультимедийные контейнеры."""

    MP4 = "mp4"
    WEBM = "webm"
    OTHER = "other"


class ContainerLimit(IntEnum):
    """Пределы ограниченного разбора BMFF/EBML."""

    HEADER_BYTES = 65536
    ATOM_HEADER_BYTES = 8
    BRAND_BYTES = 4
    EXTENDED_SIZE_BYTES = 8
    EBML_ID_BYTES = 4
    EBML_SIZE_BYTES = 8
    OCTET_BITS = 8
    HIGH_BIT = 128
    EBML_HEADER = 0x1A45DFA3
    EBML_DOCTYPE = 0x4282


class ContainerBrand(StrEnum):
    """Разрешённые major brands; MOV/3GP не входят в whitelist."""

    ISOM = "isom"
    ISO2 = "iso2"
    ISO3 = "iso3"
    ISO4 = "iso4"
    ISO5 = "iso5"
    ISO6 = "iso6"
    MP41 = "mp41"
    MP42 = "mp42"
    AVC1 = "avc1"
    DASH = "dash"
    M4A = "M4A "
    M4V = "M4V "


def _vint(data: bytes, offset: int, *, identifier: bool) -> tuple[int, int] | None:
    """Прочитать ограниченный EBML variable integer."""
    if offset >= len(data) or data[offset] == Number.ZERO:
        return None
    marker: int = ContainerLimit.HIGH_BIT
    size: int = Number.ONE
    maximum = ContainerLimit.EBML_ID_BYTES if identifier else ContainerLimit.EBML_SIZE_BYTES
    while not data[offset] & marker:
        marker >>= Number.ONE
        size += Number.ONE
    if size > maximum or offset + size > len(data):
        return None
    value = data[offset] if identifier else data[offset] & (marker - Number.ONE)
    for byte in data[offset + Number.ONE : offset + size]:
        value = (value << ContainerLimit.OCTET_BITS) | byte
    return value, offset + size


def _webm(data: bytes) -> bool:
    """Проверить реальный EBML DocType, исключая Matroska."""
    header = _vint(data, Number.ZERO, identifier=True)
    if header is None or header[Number.ZERO] != ContainerLimit.EBML_HEADER:
        return False
    length = _vint(data, header[Number.ONE], identifier=False)
    if length is None:
        return False
    offset = length[Number.ONE]
    end = offset + length[Number.ZERO]
    if end > len(data):
        return False
    while offset < end:
        identifier = _vint(data, offset, identifier=True)
        if identifier is None:
            return False
        length = _vint(data, identifier[Number.ONE], identifier=False)
        if length is None:
            return False
        start = length[Number.ONE]
        offset = start + length[Number.ZERO]
        if offset > end:
            return False
        if identifier[Number.ZERO] == ContainerLimit.EBML_DOCTYPE:
            return data[start:offset] == ContainerKind.WEBM.encode()
    return False


def identify_container(path: Path) -> ContainerKind:
    """Проверить bounded prefix без доверия расширению или ffprobe aliases.

    Args:
        path: Проверяемый локальный файл.
    """
    with path.open("rb") as source:
        data = source.read(ContainerLimit.HEADER_BYTES)
    if _webm(data):
        return ContainerKind.WEBM
    offset: int = Number.ZERO
    while offset + ContainerLimit.ATOM_HEADER_BYTES <= len(data):
        size = int.from_bytes(data[offset : offset + ContainerLimit.BRAND_BYTES], "big")
        atom = data[offset + ContainerLimit.BRAND_BYTES : offset + ContainerLimit.ATOM_HEADER_BYTES]
        start = offset + ContainerLimit.ATOM_HEADER_BYTES
        if size == Number.ONE:
            if start + ContainerLimit.EXTENDED_SIZE_BYTES > len(data):
                return ContainerKind.OTHER
            size = int.from_bytes(data[start : start + ContainerLimit.EXTENDED_SIZE_BYTES], "big")
            start += ContainerLimit.EXTENDED_SIZE_BYTES
        if size < start - offset or offset + size > len(data):
            return ContainerKind.OTHER
        if atom == b"ftyp":
            major = data[start : start + ContainerLimit.BRAND_BYTES].decode(
                "ascii", errors="replace"
            )
            return (
                ContainerKind.MP4
                if any(major == brand for brand in ContainerBrand)
                else ContainerKind.OTHER
            )
        offset += size
    return ContainerKind.OTHER
