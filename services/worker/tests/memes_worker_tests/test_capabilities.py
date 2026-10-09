import json
from pathlib import Path

import pytest

from memes_worker.application.dto.runtime import WorkerExecutionError
from memes_worker.infrastructure.config.settings import WorkerSettings, WorkerText
from memes_worker.infrastructure.ml.manifest import write_manifest


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_adapter", "clustering"),
        ("preprocessing", "different-sampling"),
        ("dimensions", 256),
        ("space", "another-space"),
        ("extra_field", "unexpected"),
    ],
)
def test_custom_passport_cannot_claim_an_unexecuted_adapter(
    worker_configuration: dict[str, object], field: str, value: object
) -> None:
    version: dict[str, object] = {
        "model": "ffmpeg",
        "weights": "builtin-v1",
        "task_adapter": WorkerText.ADAPTER,
        "preprocessing": WorkerText.PREPROCESSING,
        "dimensions": None,
        "space": None,
    }
    version[field] = value
    settings = WorkerSettings.model_validate(
        {
            **worker_configuration,
            "model_versions_json": json.dumps({"preview": version}),
        }
    )
    with pytest.raises(WorkerExecutionError):
        settings.capabilities()


def test_embedding_passport_dimension_must_match_executed_configuration(
    worker_configuration: dict[str, object], pinned_model: Path
) -> None:
    write_manifest(pinned_model, model=WorkerText.MODEL, revision=WorkerText.WEIGHTS)
    version = {
        "model": WorkerText.MODEL,
        "weights": WorkerText.WEIGHTS,
        "task_adapter": WorkerText.ADAPTER,
        "preprocessing": WorkerText.PREPROCESSING,
        "dimensions": 256,
        "space": WorkerText.SPACE,
    }
    settings = WorkerSettings.model_validate(
        {
            **worker_configuration,
            "operations": "text_embedding",
            "jina_model_directory": str(pinned_model),
            "model_versions_json": json.dumps({"text_embedding": version}),
        }
    )
    with pytest.raises(WorkerExecutionError):
        settings.capabilities()
