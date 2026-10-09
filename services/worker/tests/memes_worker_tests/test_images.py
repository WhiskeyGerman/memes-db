import json
import sys
from pathlib import Path

import pytest
from PIL import Image

from memes_worker.infrastructure.ml.process import execute


@pytest.mark.asyncio
@pytest.mark.parametrize("format_name", ["PNG", "GIF", "WEBP"])
async def test_animation_samples_first_and_last_composed_frames(
    animation_file: Path, format_name: str, tmp_path: Path
) -> None:
    request = tmp_path / "input.json"
    response = tmp_path / "output.json"
    request.write_text(
        json.dumps(
            {"source": str(animation_file), "directory": str(tmp_path), "operation": "frames"}
        ),
        encoding="utf-8",
    )
    await execute(
        [
            sys.executable,
            "-m",
            "memes_worker.infrastructure.ml.images",
            str(request),
            str(response),
        ],
        timeout_seconds=10,
        directory=tmp_path,
    )
    frames = json.loads(response.read_text(encoding="utf-8"))["frames"]
    assert frames[0]["span"]["start_ms"] == 0
    assert frames[-1]["span"]["end_ms"] == 1000
    with Image.open(frames[0]["path"]) as first, Image.open(frames[-1]["path"]) as last:
        first_pixel, last_pixel = first.getpixel((0, 0)), last.getpixel((0, 0))
        assert isinstance(first_pixel, tuple) and isinstance(last_pixel, tuple)
        assert first_pixel[0] > 200
        assert last_pixel[2] > 200


@pytest.mark.asyncio
async def test_apng_poster_does_not_enter_animation_timeline(
    apng_poster: Path, tmp_path: Path
) -> None:
    request = tmp_path / "input.json"
    response = tmp_path / "output.json"
    request.write_text(
        json.dumps({"source": str(apng_poster), "directory": str(tmp_path), "operation": "frames"}),
        encoding="utf-8",
    )
    await execute(
        [
            sys.executable,
            "-m",
            "memes_worker.infrastructure.ml.images",
            str(request),
            str(response),
        ],
        timeout_seconds=10,
        directory=tmp_path,
    )
    frames = json.loads(response.read_text(encoding="utf-8"))["frames"]
    assert frames[-1]["span"]["end_ms"] == 1000
    with Image.open(frames[0]["path"]) as first:
        pixel = first.getpixel((0, 0))
        assert isinstance(pixel, tuple) and len(pixel) == 3
        red, green, blue = pixel
        assert green > 100 and red < 30 and blue < 30
