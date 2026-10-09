"""Изолированный Build адаптер загрузки закрепленной HF ревизии."""

import argparse
from enum import StrEnum
from pathlib import Path

from huggingface_hub import hf_hub_download, snapshot_download

from memes_worker.infrastructure.config.settings import WorkerText
from memes_worker.infrastructure.ml.manifest import write_manifest
from memes_worker.infrastructure.ml.model_config import EmbeddingCapacity, validate_model_capacity


class DownloadedModel(StrEnum):
    """Известные официальные источники; произвольный repo не исполняется."""

    JINA = "jina"
    ASR = "asr"


def main() -> None:
    """Вход относится к административной стадии сборки, а не worker заданиям."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, choices=list(DownloadedModel))
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--dimensions", type=int, default=EmbeddingCapacity.DIMENSIONS)
    arguments = parser.parse_args()
    model: str = WorkerText.MODEL
    revision: str = WorkerText.WEIGHTS
    if arguments.model == DownloadedModel.ASR:
        model, revision = "Systran/faster-whisper-small", WorkerText.ASR_WEIGHTS
    if arguments.model == DownloadedModel.JINA:
        if not 0 < arguments.dimensions <= EmbeddingCapacity.DIMENSIONS:
            raise ValueError("embedding_dimensions_exceed_model_capacity")
        hf_hub_download(
            repo_id=model,
            revision=revision,
            filename="config.json",
            local_dir=arguments.directory,
        )
        validate_model_capacity(arguments.directory, dimensions=arguments.dimensions)
    snapshot_download(repo_id=model, revision=revision, local_dir=arguments.directory)
    write_manifest(
        arguments.directory, model=model, revision=revision, dimensions=arguments.dimensions
    )


if __name__ == "__main__":
    main()
