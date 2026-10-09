import json
import os
import subprocess
from pathlib import Path

import pytest

from memes_worker_tests.ml_smoke import (
    SmokeConfiguration,
    SmokeEnvironment,
    SmokeLimit,
    SmokeOutput,
)


@pytest.mark.worker_ml
@pytest.mark.acceptance
def test_cold_production_ocr_module_loads_both_languages(
    smoke_configuration: SmokeConfiguration,
) -> None:
    root = os.environ.get(SmokeEnvironment.OCR)
    if not root:
        pytest.fail("MEMES_WORKER_SMOKE_OCR is required for actual OCR smoke")
    models = Path(root).resolve()
    if not all((models / language).is_dir() for language in ("detection", "ru", "en")):
        pytest.fail("actual OCR detector and both recognition artifacts are required")
    script = Path(__file__).parent / "fixtures" / "ocr_smoke.py"
    completed = subprocess.run(  # noqa: S603 -- Закрытый image, readonly artifacts, без shell.
        [
            smoke_configuration.docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cpus",
            "4",
            "--memory",
            "6g",
            "--pids-limit",
            "256",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--tmpfs",
            "/tmp:size=1g,mode=1777",  # noqa: S108 -- Отдельный tmpfs контейнера.
            "--tmpfs",
            "/var/cache/memes-worker:size=1g,uid=10001,gid=10001",
            "-v",
            f"{models.as_posix()}:/models/ocr:ro",
            "-v",
            f"{smoke_configuration.worker_source.as_posix()}:/app/services/worker/src:ro",
            "-v",
            f"{script.as_posix()}:/smoke.py:ro",
            "--entrypoint",
            "python",
            smoke_configuration.image,
            "/smoke.py",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SmokeLimit.TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    summaries = [
        json.loads(line.removeprefix(SmokeOutput.PREFIX))
        for line in completed.stdout.splitlines()
        if line.startswith(SmokeOutput.PREFIX)
    ]
    assert len(summaries) == 1
    assert summaries[0]["fragments"] >= 2
    assert summaries[0]["languages"] == ["en", "ru"]
