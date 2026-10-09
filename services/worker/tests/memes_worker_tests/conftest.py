import json
import os
import shutil
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from os import environ
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from _pytest.terminal import TerminalReporter
from PIL import Image

from memes_kernel.values import FileRef, Sha256
from memes_worker.application.dto.runtime import WorkAssignment, WorkResult
from memes_worker.application.execution import Assignment, ExecutionKey, Operation
from memes_worker.infrastructure.cache.sqlite import SQLiteCache
from memes_worker.infrastructure.evaluation.json import JSONEvaluationDataset
from memes_worker_tests.configuration_fixtures import configuration_markers as configuration_markers
from memes_worker_tests.configuration_fixtures import service_environment as service_environment
from memes_worker_tests.domain_fixtures import (
    now as now,
)
from memes_worker_tests.domain_fixtures import (
    original_file as original_file,
)
from memes_worker_tests.ml_smoke import SmokeConfiguration, SmokeEnvironment


@pytest.fixture
def work(now: datetime) -> WorkAssignment:
    key = ExecutionKey(
        source_id=uuid4(),
        source_hash=Sha256(value="a" * 64),
        cycle_id=uuid4(),
        operation=Operation.OCR,
        fragment_id=uuid4(),
        model_revision="model-v1",
        preprocessing_revision="sampling-v1",
    )
    return WorkAssignment(
        assignment=Assignment(
            task_id=uuid4(),
            attempt_id=uuid4(),
            source=FileRef(id=uuid4(), sha256=key.source_hash, size_bytes=128),
            key=key,
            lease_expires_at=now + timedelta(seconds=120),
            deadline=now + timedelta(seconds=600),
        ),
        source_type="media",
        media_kind="image",
        feature_version={"model": "test"},
        download_url="https://objects.invalid/input",
        upload_url="https://objects.invalid/result",
        preview_upload_url="https://objects.invalid/preview",
        preview_key="attempts/test/preview.jpg",
    )


@pytest.fixture
def cache(tmp_path: Path) -> SQLiteCache:
    return SQLiteCache(tmp_path, maximum_bytes=2_147_483_648)


@pytest.fixture
def result(work: WorkAssignment) -> WorkResult:
    return WorkResult(key=work.assignment.key, payload={"kind": "ocr", "result": {"fragments": []}})


@pytest.fixture
def animation_file(format_name: str, tmp_path: Path) -> Path:
    frames = [Image.new("RGB", (16, 16), color) for color in ("red", "blue")]
    target = tmp_path / "animation"
    frames[0].save(
        target,
        format=format_name,
        save_all=True,
        append_images=frames[1:],
        duration=[500, 500],
        loop=0,
    )
    return target


@pytest.fixture
def apng_poster(tmp_path: Path) -> Path:
    target = tmp_path / "poster.png"
    poster = Image.new("RGB", (16, 16), "red")
    frames = [Image.new("RGB", (16, 16), color) for color in ("green", "blue")]
    poster.save(
        target,
        format="PNG",
        save_all=True,
        append_images=frames,
        default_image=True,
        duration=[500, 500],
        loop=0,
    )
    return target


@pytest.fixture
def quality_dataset(tmp_path: Path) -> JSONEvaluationDataset:
    pairs = []
    for name, split, positive, similarity, rank in (
        ("cal-positive", "calibration", True, 0.8, 1),
        ("cal-negative", "calibration", False, 0.7, 2),
        ("eval-positive", "evaluation", True, 0.85, 1),
        ("eval-negative", "evaluation", False, 0.9, 2),
    ):
        pairs.append(
            {
                "source_id": name,
                "candidate_id": name + "-candidate",
                "source_family": name,
                "candidate_family": name + "-family",
                "modality": "image",
                "language": "ru",
                "split": split,
                "positive": positive,
                "similarity": similarity,
                "rank": rank,
            }
        )
    data = {
        "pairs": pairs,
        "queries": [
            {
                "id": "query",
                "language": "ru",
                "media_kind": "image",
                "relevance": {"relevant": 3},
                "ranking": ["irrelevant", "relevant"],
            }
        ],
        "texts": [
            {
                "kind": "ocr",
                "language": "ru",
                "media_kind": "image",
                "reference": "кот",
                "prediction": "кит",
            },
            {
                "kind": "transcript",
                "language": "en",
                "media_kind": "audio",
                "reference": "one two",
                "prediction": "one three",
            },
        ],
    }
    path = tmp_path / "quality.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return JSONEvaluationDataset(path)


@pytest.fixture
def pinned_model(tmp_path: Path) -> Path:
    source = Path(__file__).parent / "fixtures" / "jina-omni-nano-009e0d09-config.json"
    target = tmp_path / "model"
    target.mkdir()
    shutil.copyfile(source, target / "config.json")
    return target


@pytest.fixture
def worker_configuration(tmp_path: Path) -> dict[str, object]:
    return {
        "coordinator_url": "https://processing.invalid",
        "worker_id": str(uuid4()),
        "token": uuid4().hex + uuid4().hex,
        "release_id": "test",
        "cache_directory": str(tmp_path / "cache"),
    }


@pytest.fixture
def descendant_process_script(tmp_path: Path) -> Path:
    script = tmp_path / "descendant.py"
    script.write_text(
        "import signal\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "def stopped(signum, frame):\n"
        "    Path(sys.argv[1]).write_text('terminated', encoding='utf-8')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stopped)\n"
        "Path(sys.argv[2]).write_text('ready', encoding='utf-8')\n"
        "time.sleep(20)\n",
        encoding="utf-8",
    )
    return script


@pytest.fixture
def smoke_configuration() -> SmokeConfiguration:
    image = os.environ.get(SmokeEnvironment.IMAGE)
    model_path = os.environ.get(SmokeEnvironment.MODEL)
    docker = shutil.which("docker")
    if not image or not model_path or docker is None:
        pytest.fail("actual offline ML smoke requires Docker image and pinned local model")
    model = Path(model_path).resolve()
    if not (model / "config.json").is_file():
        pytest.fail("pinned model is missing config.json")
    asr_path = os.environ.get(SmokeEnvironment.ASR)
    asr = Path(asr_path).resolve() if asr_path else None
    repository = next(
        parent
        for parent in Path(__file__).resolve().parents
        if (parent / "deploy" / "compose.yaml").is_file()
    )
    return SmokeConfiguration(
        docker=docker,
        image=image,
        model=model,
        asr=asr,
        worker_source=repository / "services" / "worker" / "src",
        script=Path(__file__).parent / "fixtures" / "ml_smoke.py",
    )


@pytest.fixture
def sensitive_error() -> str:
    return "PRIVATE_QUERY https://private.invalid/token /private/media.png SECRET_TOKEN"


@pytest.fixture
def raising_action() -> Callable[[BaseException], Callable[[], None]]:
    def factory(error: BaseException) -> Callable[[], None]:
        def action() -> None:
            raise error

        return action

    return factory


@pytest.fixture
def subprocess_diagnostic_script(tmp_path: Path) -> Path:
    script = tmp_path / "diagnostic.py"
    script.write_text(
        "import sys\n"
        "from memes_worker.infrastructure.ml.diagnostics import DiagnosticCode, "
        "DiagnosticException, ProcessDiagnostic, diagnostic_line\n"
        "from memes_worker.infrastructure.ml.plugin_runtime import PluginExit\n"
        "mode = sys.argv[1]\n"
        "sys.stderr.write(sys.argv[2] + '\\n')\n"
        "if mode == 'flood':\n"
        "    sys.stderr.write('a' * 1000000 + '\\n')\n"
        "    sys.stdout.write('ok')\n"
        "    sys.exit(0)\n"
        "if mode == 'input':\n"
        "    sys.stderr.write(diagnostic_line(ProcessDiagnostic(DiagnosticCode.INPUT, "
        "DiagnosticException.INPUT)) + '\\n')\n"
        "sys.exit(PluginExit.INVALID_INPUT)\n",
        encoding="utf-8",
    )
    return script


@pytest.fixture
def active_work(work: WorkAssignment) -> WorkAssignment:
    return replace(
        work,
        assignment=replace(work.assignment, deadline=datetime.now(UTC) + timedelta(minutes=10)),
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Не допустить зелёный CI при пропущенных integration проверках."""
    if environ.get("MEMES_REQUIRE_INTEGRATION") != "1":
        return
    reporter = cast(
        TerminalReporter | None, session.config.pluginmanager.get_plugin("terminalreporter")
    )
    if reporter is not None and reporter.stats.get("skipped"):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter.write_line("Integration CI must not skip tests", red=True)
