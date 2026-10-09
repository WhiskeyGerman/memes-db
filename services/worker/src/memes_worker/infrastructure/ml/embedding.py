"""Изолированный адаптер официального Jina retrieval encoder."""

import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import cast

import numpy as np
from PIL import Image
from sentence_transformers import SentenceTransformer

from memes_worker.application.dto.runtime import JSONValue
from memes_worker.application.execution import Operation
from memes_worker.infrastructure.ml.diagnostics import (
    DiagnosticMessage,
    PluginInputError,
    PluginModelConfigurationError,
)
from memes_worker.infrastructure.ml.model_config import validate_model_capacity
from memes_worker.infrastructure.ml.plugin_runtime import entrypoint
from memes_worker.infrastructure.wire import object_payload, value_integer, value_string


class EmbeddingError(StrEnum):
    """Явные ошибки офлайн мультимодального runtime."""

    PROCESSOR_UNAVAILABLE = "multimodal_processor_unavailable"


def main() -> None:
    """Веса и доверенный model code предварительно входят в ML-артефакт."""
    request = object_payload(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
    validate_model_capacity(
        Path(value_string(request.get("jina_model"))),
        dimensions=value_integer(request.get("dimensions")),
    )
    model = SentenceTransformer(
        value_string(request.get("jina_model")),
        trust_remote_code=True,
        local_files_only=True,
        device=value_string(request.get("device")),
        model_kwargs={
            "default_task": "retrieval",
            "modality": {
                "visual_embedding": "vision",
                "audio_embedding": "audio",
                "text_embedding": "text",
            }[value_string(request.get("operation"))],
        },
    )
    if (
        Operation(value_string(request.get("operation"))) is not Operation.TEXT_EMBEDDING
        and getattr(model[0], "processor", None) is None
    ):
        raise PluginModelConfigurationError(EmbeddingError.PROCESSOR_UNAVAILABLE)
    inputs = request.get("inputs")
    if not isinstance(inputs, list):
        raise PluginInputError(DiagnosticMessage.INVALID_MODEL_INPUT)
    results: list[JSONValue] = []
    is_query = request.get("source_type") in {"query", "sample"}
    for raw in inputs:
        item = object_payload(raw)
        value: object = (
            value_string(item.get("text") if "text" in item else item.get("path"))
            if "frames" not in item
            else None
        )
        frame_paths = item.get("frames")
        if isinstance(frame_paths, list):
            arrays = []
            for frame_path in frame_paths:
                with Image.open(value_string(frame_path)) as frame:
                    arrays.append(np.asarray(frame.convert("RGB")))
            value = np.stack(arrays)
        vector = (
            model.encode_query(value, truncate_dim=value_integer(request.get("dimensions")))
            if is_query
            else model.encode_document(value, truncate_dim=value_integer(request.get("dimensions")))
        )
        values = cast(list[float], vector.tolist())
        results.append({"values": list(values), "span": item.get("span")})
    Path(sys.argv[2]).write_text(json.dumps({"items": results}, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    entrypoint(main)
