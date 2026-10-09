"""Независимая проверка производного JPEG без доверия устройству."""

import asyncio
from enum import IntEnum, StrEnum
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from memes_processing.application.ports.external import IPreviewVerifier


class PreviewLimit(IntEnum):
    """Пределы публичного превью."""

    SIDE = 512
    BYTES = 2097152


class PreviewFormat(StrEnum):
    """Единственный производный браузерный формат."""

    JPEG = "JPEG"


def verify_jpeg(data: bytes) -> None:
    """Проверить кодек, размер, структуру и фактическую декодируемость."""
    if not data or len(data) > PreviewLimit.BYTES:
        raise ValueError("invalid_preview_size")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format != PreviewFormat.JPEG or max(image.size) > PreviewLimit.SIDE:
                raise ValueError("invalid_preview_format")
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ValueError("invalid_preview_bytes") from error


class PillowPreviewVerifier(IPreviewVerifier):
    """Координатор проверяет полученный файл вне транзакции БД."""

    async def verify(self, data: bytes) -> None:
        """Не блокировать HTTP event loop при декодировании."""
        await asyncio.to_thread(verify_jpeg, data)
