from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path


class SmokeEnvironment(StrEnum):
    IMAGE = "MEMES_WORKER_SMOKE_IMAGE"
    MODEL = "MEMES_WORKER_SMOKE_MODEL"
    ASR = "MEMES_WORKER_SMOKE_ASR"
    OCR = "MEMES_WORKER_SMOKE_OCR"


class SmokeLimit(IntEnum):
    TIMEOUT_SECONDS = 180
    DIMENSIONS = 768
    TRUNCATED_DIMENSIONS = 512
    DURATION_MILLISECONDS = 1_000


class SmokeOutput(StrEnum):
    PREFIX = "SMOKE:"


@dataclass(frozen=True, slots=True, kw_only=True)
class SmokeConfiguration:
    docker: str
    image: str
    model: Path
    asr: Path | None
    worker_source: Path
    script: Path
