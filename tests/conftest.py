from os import environ
from typing import cast

import pytest
from _pytest.terminal import TerminalReporter


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
