from pathlib import Path

import pytest
from pydantic import ValidationError

from memes_worker.infrastructure.config.settings import WorkerSettings
from memes_worker.infrastructure.ml.diagnostics import PluginModelConfigurationError
from memes_worker.infrastructure.ml.manifest import write_manifest
from memes_worker.infrastructure.ml.model_config import validate_model_capacity


def test_actual_pinned_config_has_768_dimension_capacity(pinned_model: Path) -> None:
    assert validate_model_capacity(pinned_model, dimensions=768) == 768
    assert validate_model_capacity(pinned_model, dimensions=512) == 768
    with pytest.raises(PluginModelConfigurationError):
        validate_model_capacity(pinned_model, dimensions=2048)
    with pytest.raises(PluginModelConfigurationError):
        write_manifest(
            pinned_model,
            model="jinaai/jina-embeddings-v5-omni-nano",
            revision="009e0d09a98a82227526e8c5aa7b0aa282e36163",
            dimensions=2048,
        )


def test_impossible_dimension_fails_before_loading_models(
    worker_configuration: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        WorkerSettings.model_validate({**worker_configuration, "embedding_dimensions": 2048})
