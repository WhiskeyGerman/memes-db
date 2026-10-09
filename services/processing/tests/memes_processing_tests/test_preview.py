from io import BytesIO

import pytest
from PIL import Image

from memes_processing.infrastructure.storage.preview import verify_jpeg


def test_preview_verification_checks_codec_and_decode(jpeg_preview: bytes) -> None:
    verify_jpeg(jpeg_preview)
    with pytest.raises(ValueError, match="invalid_preview_bytes"):
        verify_jpeg(jpeg_preview[:20])
    output = BytesIO()
    Image.new("RGB", (32, 32)).save(output, "PNG")
    with pytest.raises(ValueError, match="invalid_preview_format"):
        verify_jpeg(output.getvalue())


def test_preview_rejects_oversized_dimensions() -> None:
    output = BytesIO()
    Image.new("RGB", (513, 16)).save(output, "JPEG")
    with pytest.raises(ValueError, match="invalid_preview_format"):
        verify_jpeg(output.getvalue())
