"""Проверка фиксированной ревизии и контрольных сумм локальных весов."""

import json
from enum import IntEnum, StrEnum
from hashlib import sha256
from pathlib import Path

from memes_worker.application.dto.runtime import WorkerExecutionError, WorkerFailure
from memes_worker.infrastructure.ml.model_config import (
    EmbeddingCapacity,
    EmbeddingSnapshot,
    validate_model_capacity,
)
from memes_worker.infrastructure.wire import object_payload, value_string


class ManifestName(StrEnum):
    """Контракт ML-артефакта стадии Build."""

    FILE = "model-manifest.json"


class ManifestLimit(IntEnum):
    """Размер буфера проверки не зависит от объема весов."""

    CHUNK_BYTES = 1048576


def digest_file(path: Path) -> str:
    """Проверить файл потоково без загрузки весов в RAM."""
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(ManifestLimit.CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(
    directory: Path, *, model: str, revision: str, dimensions: int = EmbeddingCapacity.DIMENSIONS
) -> None:
    """Одноразовая Build задача фиксирует содержимое исходного snapshot."""
    root = directory.resolve()
    if model == EmbeddingSnapshot.MODEL:
        validate_model_capacity(root, dimensions=dimensions)
    files = {
        str(path.relative_to(root).as_posix()): digest_file(path)
        for path in root.rglob("*")
        if path.is_file() and path.name != ManifestName.FILE and ".cache" not in path.parts
    }
    if not files:
        raise WorkerExecutionError(WorkerFailure.INVALID_INPUT)
    (root / ManifestName.FILE).write_text(
        json.dumps(
            {
                "model": model,
                "revision": revision,
                "files": files,
                "embedding_capacity": EmbeddingCapacity.DIMENSIONS
                if model == EmbeddingSnapshot.MODEL
                else None,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def verify_manifest(directory: Path, *, model: str, revision: str) -> None:
    """Не объявлять произвольную ревизию на основании одного имени каталога."""
    try:
        root = directory.resolve()
        manifest = object_payload(
            json.loads((root / ManifestName.FILE).read_text(encoding="utf-8"))
        )
        if manifest.get("model") != model or manifest.get("revision") != revision:
            raise WorkerExecutionError(WorkerFailure.INVALID_INPUT)
        files = object_payload(manifest.get("files"))
        if not files:
            raise WorkerExecutionError(WorkerFailure.INVALID_INPUT)
        for relative, expected in files.items():
            target = (root / relative).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise WorkerExecutionError(WorkerFailure.INVALID_INPUT)
            if digest_file(target) != value_string(expected):
                raise WorkerExecutionError(WorkerFailure.INVALID_INPUT)
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError) as error:
        raise WorkerExecutionError(WorkerFailure.INVALID_INPUT) from error
