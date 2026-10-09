from os import environ
from typing import cast

import pytest
from _pytest.terminal import TerminalReporter

from memes_processing_tests.configuration_fixtures import service_environment as service_environment
from memes_processing_tests.domain_fixtures import extraction as extraction
from memes_processing_tests.domain_fixtures import now as now
from memes_processing_tests.domain_fixtures import original_file as original_file
from memes_processing_tests.domain_fixtures import task as task
from memes_processing_tests.domain_fixtures import text_version as text_version
from memes_processing_tests.domain_fixtures import vector_version as vector_version
from memes_processing_tests.fixtures import (
    coordinator as coordinator,
)
from memes_processing_tests.fixtures import (
    jpeg_preview as jpeg_preview,
)
from memes_processing_tests.fixtures import (
    memory_artifacts as memory_artifacts,
)
from memes_processing_tests.fixtures import (
    memory_delivery as memory_delivery,
)
from memes_processing_tests.fixtures import (
    private_sample as private_sample,
)
from memes_processing_tests.fixtures import (
    processing_http as processing_http,
)
from memes_processing_tests.fixtures import (
    queued_transaction as queued_transaction,
)
from memes_processing_tests.fixtures import (
    sample_feature_results as sample_feature_results,
)
from memes_processing_tests.fixtures import (
    source as source,
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
