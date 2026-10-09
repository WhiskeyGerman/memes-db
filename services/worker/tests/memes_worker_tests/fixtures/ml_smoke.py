"""Реальный офлайн smoke для закреплённых мультимодальных весов."""

import json
import math
import subprocess
import sys
import wave
from enum import IntEnum, StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from PIL import Image

from memes_worker.infrastructure.ml.asr import main as asr_main
from memes_worker.infrastructure.ml.embedding import main as embedding_main


class SmokeLimit(IntEnum):
    """Небольшие синтетические входы функциональной проверки."""

    IMAGE_SIDE = 64
    SAMPLE_RATE = 16_000
    FREQUENCY = 440
    SAMPLE_BYTES = 2
    MILLISECONDS = 1_000
    FFT_THREADS = 1


class SmokeCase(StrEnum):
    """Проверяемые настоящие маршруты модели."""

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    ANIMATION = "animation"
    ASR = "asr"


def execute_smoke(directory: Path) -> None:
    """Проверить модель без сети и раскрытия самого embedding."""
    case = SmokeCase(sys.argv[1])
    dimensions = int(sys.argv[2])
    image_path = directory / "image.png"
    Image.new("RGB", (SmokeLimit.IMAGE_SIDE, SmokeLimit.IMAGE_SIDE), "red").save(image_path)
    request: dict[str, object] = {
        "jina_model": "/models/jina",
        "asr_model": "/models/asr",
        "device": "cpu",
        "dimensions": dimensions,
        "source_type": "sample",
    }
    span = {"start_ms": 0, "end_ms": SmokeLimit.MILLISECONDS}
    if case is SmokeCase.TEXT:
        operation = "text_embedding"
        inputs: list[object] = [{"text": "A red square", "span": None}]
    elif case in {SmokeCase.AUDIO, SmokeCase.ASR}:
        timeline = np.arange(SmokeLimit.SAMPLE_RATE, dtype=np.float32)
        amplitude = (
            np.zeros_like(timeline)
            if case is SmokeCase.ASR
            else np.sin(timeline * (2 * math.pi * SmokeLimit.FREQUENCY / SmokeLimit.SAMPLE_RATE))
        )
        samples = (amplitude * np.iinfo(np.int16).max).astype(np.int16)
        audio_path = directory / "audio.wav"
        with wave.open(str(audio_path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(SmokeLimit.SAMPLE_BYTES)
            audio.setframerate(SmokeLimit.SAMPLE_RATE)
            audio.writeframes(samples.tobytes())
        operation = "transcript" if case is SmokeCase.ASR else "audio_embedding"
        inputs = [{"path": str(audio_path), "span": span}]
    elif case is SmokeCase.VIDEO:
        video_path = directory / "video.mp4"
        subprocess.run(  # noqa: S603 -- Фиксированный ffmpeg без shell и внешних входов.
            [
                "/usr/bin/ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-threads",
                str(SmokeLimit.FFT_THREADS),
                "-loop",
                "1",
                "-i",
                str(image_path),
                "-t",
                "1",
                "-vf",
                "fps=4",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(video_path),
            ],
            check=True,
            timeout=30,
        )
        operation = "visual_embedding"
        inputs = [{"path": str(video_path), "span": span}]
    elif case is SmokeCase.ANIMATION:
        last_path = directory / "last.png"
        Image.new("RGB", (SmokeLimit.IMAGE_SIDE, SmokeLimit.IMAGE_SIDE), "blue").save(last_path)
        operation = "visual_embedding"
        inputs = [{"frames": [str(image_path), str(last_path)], "span": span}]
    else:
        operation = "visual_embedding"
        inputs = [{"path": str(image_path), "span": None}]
    request.update({"operation": operation, "inputs": inputs})
    request_path = directory / "request.json"
    response_path = directory / "result.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    sys.argv = ["plugin", str(request_path), str(response_path)]
    (asr_main if case is SmokeCase.ASR else embedding_main)()
    response = json.loads(response_path.read_text(encoding="utf-8"))
    if case is SmokeCase.ASR:
        summary = {"case": case, "fragments": response["fragments"]}
    else:
        values = response["items"][0]["values"]
        summary = {
            "case": case,
            "dimensions": len(values),
            "finite": all(math.isfinite(value) for value in values),
            "norm": math.sqrt(sum(value * value for value in values)),
            "span": response["items"][0]["span"],
        }
    sys.stdout.write("SMOKE:" + json.dumps(summary) + "\n")


def main() -> None:
    """Создать изолированный каталог и удалить синтетические файлы."""
    with TemporaryDirectory(prefix="memes-ml-smoke-") as directory:
        execute_smoke(Path(directory))


if __name__ == "__main__":
    main()
