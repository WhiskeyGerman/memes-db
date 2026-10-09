from pathlib import Path

import pytest

from memes_worker.infrastructure.ml.diagnostics import PluginModelConfigurationError
from memes_worker.infrastructure.ml.manifest import write_manifest
from memes_worker.infrastructure.ml.ocr_config import (
    OCRArtifact,
    ocr_arguments,
    validate_ocr_directory,
)


@pytest.fixture
def local_ocr_directory(tmp_path: Path) -> Path:
    root = tmp_path / "ocr"
    root.mkdir()
    for name in OCRArtifact:
        (root / name).write_bytes(b"test-inference-artifact")
    write_manifest(root, model="PaddleOCR", revision="PP-OCRv4-ru-en@2.10.0")
    return root


def test_disabled_classifier_uses_existing_local_inference_files(
    local_ocr_directory: Path,
) -> None:
    arguments = ocr_arguments(
        language="en",
        detection=local_ocr_directory,
        recognition=local_ocr_directory,
        use_gpu=False,
    )
    assert arguments["use_angle_cls"] is False
    assert arguments["cls_model_dir"] == arguments["det_model_dir"]
    validate_ocr_directory(Path(str(arguments["cls_model_dir"])))


def test_a_manifest_without_real_inference_file_names_is_not_an_ocr_artifact(
    pinned_model: Path,
) -> None:
    write_manifest(pinned_model, model="PaddleOCR", revision="PP-OCRv4-ru-en@2.10.0")
    with pytest.raises(PluginModelConfigurationError):
        validate_ocr_directory(pinned_model)


def test_empty_model_weights_fail_before_polling(local_ocr_directory: Path) -> None:
    (local_ocr_directory / OCRArtifact.PARAMETERS).write_bytes(b"")
    with pytest.raises(PluginModelConfigurationError):
        validate_ocr_directory(local_ocr_directory)
