from os import environ
from typing import cast

import pytest
from _pytest.terminal import TerminalReporter

from memes_platform_tests.configuration_fixtures import (
    configuration_markers as configuration_markers,
)
from memes_platform_tests.configuration_fixtures import service_environment as service_environment
from memes_platform_tests.logging_fixtures import (
    chained_exception_record as chained_exception_record,
)
from memes_platform_tests.logging_fixtures import (
    exception_record as exception_record,
)
from memes_platform_tests.logging_fixtures import (
    json_formatter as json_formatter,
)
from memes_platform_tests.logging_fixtures import (
    logging_client as logging_client,
)
from memes_platform_tests.logging_fixtures import (
    sensitive_value as sensitive_value,
)
from memes_platform_tests.logging_fixtures import (
    statement_exception_record as statement_exception_record,
)
from memes_platform_tests.logging_fixtures import (
    validation_exception_record as validation_exception_record,
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
