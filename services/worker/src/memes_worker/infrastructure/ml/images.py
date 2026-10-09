"""Потоковое композиционное декодирование GIF/WebP/APNG в отдельном процессе."""

import json
import math
import sys
from enum import IntEnum, StrEnum
from pathlib import Path

from PIL import Image

from memes_worker.application.dto.runtime import JSONValue, Payload
from memes_worker.infrastructure.ml.diagnostics import PluginInputError
from memes_worker.infrastructure.ml.plugin_runtime import entrypoint
from memes_worker.infrastructure.wire import object_payload, value_string


class ImageLimit(IntEnum):
    """Пределы одного входа и выборки."""

    PIXELS = 40000000
    SIDE = 16384
    FRAMES = 18000
    DURATION_MS = 300000
    SAMPLES = 600
    FPS = 2
    MILLISECONDS = 1000
    DEFAULT_FRAME_MS = 100
    PREVIEW_SIDE = 512
    COLOR_MAX = 255


class ImageMode(StrEnum):
    """Фиксированные режимы производного изображения."""

    RGBA = "RGBA"
    RGB = "RGB"
    JPEG = "JPEG"
    PREVIEW = "preview"
    FRAMES = "frames"
    PNG = "PNG"


def first_frame(image: Image.Image) -> int:
    """Постер APNG не принадлежит временному циклу анимации."""
    return int(image.format == ImageMode.PNG and image.info.get("default_image") is True)


def duration(image: Image.Image) -> int:
    """Учесть ровно один цикл, игнорируя повтор проигрывания."""
    total = 0
    frames = getattr(image, "n_frames", 1)
    if frames > ImageLimit.FRAMES:
        raise PluginInputError("too_many_animation_frames")
    for index in range(first_frame(image), frames):
        image.seek(index)
        image.load()
        raw = image.info.get("duration", ImageLimit.DEFAULT_FRAME_MS)
        milliseconds = max(1, math.ceil(float(raw)))
        total += milliseconds
        if total > ImageLimit.DURATION_MS:
            raise PluginInputError("animation_too_long")
    image.seek(first_frame(image))
    return total


def save_frame(image: Image.Image, path: Path) -> None:
    """Pillow seek уже учитывает прозрачность и disposal предыдущего кадра."""
    rgba = image.convert(ImageMode.RGBA)
    background = Image.new(ImageMode.RGBA, rgba.size, (ImageLimit.COLOR_MAX,) * 4)
    composited = Image.alpha_composite(background, rgba).convert(ImageMode.RGB)
    composited.thumbnail(
        (ImageLimit.PREVIEW_SIDE, ImageLimit.PREVIEW_SIDE), Image.Resampling.LANCZOS
    )
    composited.save(path, format=ImageMode.JPEG)


def main() -> None:
    """Сформировать ограниченное число полностью скомпонованных кадров."""
    request = object_payload(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    source = Path(value_string(request.get("source")))
    directory = Path(value_string(request.get("directory")))
    operation = value_string(request.get("operation"))
    with Image.open(source) as image:
        if image.format not in {"JPEG", "PNG", "WEBP", "AVIF", "GIF"}:
            raise PluginInputError("unsupported_image_format")
        if (
            image.width > ImageLimit.SIDE
            or image.height > ImageLimit.SIDE
            or image.width * image.height > ImageLimit.PIXELS
        ):
            raise PluginInputError("image_too_large")
        if operation == ImageMode.PREVIEW:
            target = directory / "preview.jpg"
            image.seek(first_frame(image))
            save_frame(image, target)
            result: Payload = {"path": str(target)}
        elif getattr(image, "n_frames", 1) - first_frame(image) <= 1:
            target = directory / "frame-000000.jpg"
            save_frame(image, target)
            result = {"frames": [{"path": str(target), "span": None}]}
        else:
            total = duration(image)
            count = min(
                ImageLimit.SAMPLES,
                max(2, math.ceil(total / ImageLimit.MILLISECONDS * ImageLimit.FPS) + 1),
            )
            positions = sorted({round(index * (total - 1) / (count - 1)) for index in range(count)})
            frames: list[JSONValue] = []
            index, cursor, start = first_frame(image), 0, 0
            while cursor < len(positions) and index < getattr(image, "n_frames", 1):
                image.seek(index)
                image.load()
                end = start + max(
                    1, math.ceil(float(image.info.get("duration", ImageLimit.DEFAULT_FRAME_MS)))
                )
                while cursor < len(positions) and positions[cursor] < end:
                    target = directory / f"frame-{cursor:06d}.jpg"
                    save_frame(image, target)
                    frames.append(
                        {
                            "path": str(target),
                            "span": {"start_ms": start, "end_ms": min(end, total)},
                        }
                    )
                    cursor += 1
                start, index = end, index + 1
            result = {"frames": frames}
    Path(sys.argv[2]).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    entrypoint(main)
