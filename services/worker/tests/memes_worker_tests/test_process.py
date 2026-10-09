import asyncio
import sys
from pathlib import Path

import pytest

from memes_worker.application.dto.runtime import WorkerExecutionError, WorkerFailure
from memes_worker.infrastructure.ml.process import ProcessLimit, execute


@pytest.mark.asyncio
async def test_subprocess_output_is_bounded(tmp_path: Path) -> None:
    with pytest.raises(WorkerExecutionError) as error:
        await execute(
            [
                sys.executable,
                "-c",
                f"import sys; sys.stdout.write('a'*{ProcessLimit.OUTPUT_BYTES + 1})",
            ],
            timeout_seconds=5,
            directory=tmp_path,
        )
    assert error.value.reason is WorkerFailure.RESOURCE_EXHAUSTED


@pytest.mark.asyncio
async def test_subprocess_deadline_terminates_execution(tmp_path: Path) -> None:
    with pytest.raises(WorkerExecutionError) as error:
        await execute(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout_seconds=0.05,
            directory=tmp_path,
        )
    assert error.value.reason is WorkerFailure.RESOURCE_EXHAUSTED


@pytest.mark.asyncio
async def test_subprocess_cancellation_is_not_swallowed(tmp_path: Path) -> None:
    task = asyncio.create_task(
        execute(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            timeout_seconds=20,
            directory=tmp_path,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.skipif(sys.platform == "win32", reason="production process groups require POSIX")
@pytest.mark.asyncio
async def test_deadline_terminates_the_descendant_process_group(
    descendant_process_script: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "terminated"
    ready = tmp_path / "ready"
    arguments = repr([sys.executable, str(descendant_process_script), str(marker), str(ready)])
    task = asyncio.create_task(
        execute(
            [
                sys.executable,
                "-c",
                f"import subprocess,time; subprocess.Popen({arguments}); time.sleep(20)",
            ],
            timeout_seconds=5,
            directory=tmp_path,
        )
    )
    for _ in range(100):
        if ready.exists():
            break
        await asyncio.sleep(0.01)
    assert ready.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert marker.read_text(encoding="utf-8") == "terminated"
