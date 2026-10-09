"""Проверка реальной архитектуры закреплённого embedding snapshot."""

import json
from enum import IntEnum, StrEnum
from pathlib import Path

from memes_worker.infrastructure.ml.diagnostics import (
    DiagnosticMessage,
    PluginModelConfigurationError,
)
from memes_worker.infrastructure.wire import object_payload, value_integer


class EmbeddingCapacity(IntEnum):
    """Максимум из официального config nano ревизии 009e0d09."""

    DIMENSIONS = 768


class EmbeddingSnapshot(StrEnum):
    """Идентификаторы проверяемого артефакта."""

    MODEL = "jinaai/jina-embeddings-v5-omni-nano"
    TYPE = "jina_embeddings_v5_omni"
    CONFIG = "config.json"


def validate_model_capacity(directory: Path, *, dimensions: int) -> int:
    """Отклонить невозможную размерность до загрузки весов в CPU/GPU."""
    try:
        config = object_payload(
            json.loads((directory / EmbeddingSnapshot.CONFIG).read_text(encoding="utf-8"))
        )
        if config.get("model_type") != EmbeddingSnapshot.TYPE:
            raise PluginModelConfigurationError(DiagnosticMessage.INVALID_LOCAL_MODEL_ARTIFACT)
        hidden = value_integer(object_payload(config.get("text_config")).get("hidden_size"))
        raw = config.get("matryoshka_dimensions")
        if not isinstance(raw, list) or not raw:
            raise PluginModelConfigurationError(DiagnosticMessage.INVALID_LOCAL_MODEL_ARTIFACT)
        supported = tuple(value_integer(item) for item in raw)
        if (
            hidden != EmbeddingCapacity.DIMENSIONS
            or max(supported) != hidden
            or any(size <= 0 or size > hidden for size in supported)
            or dimensions not in supported
        ):
            raise PluginModelConfigurationError(DiagnosticMessage.INVALID_LOCAL_MODEL_ARTIFACT)
        return hidden
    except (OSError, json.JSONDecodeError, ValueError) as error:
        if isinstance(error, PluginModelConfigurationError):
            raise
        raise PluginModelConfigurationError(
            DiagnosticMessage.INVALID_LOCAL_MODEL_ARTIFACT
        ) from error
