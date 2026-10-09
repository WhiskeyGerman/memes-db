import json
import subprocess

import pytest

from memes_worker_tests.ml_smoke import SmokeConfiguration, SmokeLimit, SmokeOutput


@pytest.mark.worker_ml
@pytest.mark.acceptance
@pytest.mark.parametrize(
    ("case", "dimensions"),
    [
        ("text", SmokeLimit.DIMENSIONS),
        ("image", SmokeLimit.DIMENSIONS),
        ("image", SmokeLimit.TRUNCATED_DIMENSIONS),
        ("audio", SmokeLimit.DIMENSIONS),
        ("video", SmokeLimit.DIMENSIONS),
        ("animation", SmokeLimit.DIMENSIONS),
        ("asr", SmokeLimit.DIMENSIONS),
    ],
)
def test_real_offline_model_encodes_bounded_media(
    smoke_configuration: SmokeConfiguration, case: str, dimensions: int
) -> None:
    if case == "asr" and smoke_configuration.asr is None:
        pytest.fail("MEMES_WORKER_SMOKE_ASR is required for actual ASR smoke")
    arguments = [
        smoke_configuration.docker,
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,size=1g",  # noqa: S108 -- Dedicated container tmpfs; host files are read-only.
        "--memory",
        "8g",
        "--cpus",
        "4",
        "--pids-limit",
        "128",
        "-e",
        "HF_HOME=/tmp/huggingface",
        "-e",
        "HF_HUB_OFFLINE=1",
        "-e",
        "TRANSFORMERS_OFFLINE=1",
        "-e",
        "OMP_NUM_THREADS=4",
        "-v",
        f"{smoke_configuration.model.as_posix()}:/models/jina:ro",
        "-v",
        f"{smoke_configuration.worker_source.as_posix()}:/app/services/worker/src:ro",
        "-v",
        f"{smoke_configuration.script.as_posix()}:/smoke.py:ro",
    ]
    if smoke_configuration.asr is not None:
        arguments.extend(["-v", f"{smoke_configuration.asr.as_posix()}:/models/asr:ro"])
    arguments.extend(
        [
            "--entrypoint",
            "python",
            smoke_configuration.image,
            "/smoke.py",
            case,
            str(dimensions),
        ]
    )
    completed = subprocess.run(  # noqa: S603 -- Explicit image and read-only local weights; no shell.
        arguments,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=SmokeLimit.TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    summaries = [
        line.removeprefix(SmokeOutput.PREFIX)
        for line in completed.stdout.splitlines()
        if line.startswith(SmokeOutput.PREFIX)
    ]
    assert len(summaries) == 1
    result = json.loads(summaries[0])
    assert result["case"] == case
    if case == "asr":
        assert result["fragments"] == []
    else:
        assert result["dimensions"] == dimensions
        assert result["finite"]
        assert result["norm"] == pytest.approx(1.0, abs=1e-4)
        assert result["span"] == (
            None
            if case in {"image", "text"}
            else {"start_ms": 0, "end_ms": SmokeLimit.DURATION_MILLISECONDS}
        )
