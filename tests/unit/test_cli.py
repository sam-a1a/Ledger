"""The admin CLI, driven the way an operator drives it.

Covered because it is the surface someone reaches for when something is wrong,
which is exactly when a broken `--help` or a swallowed error costs the most.
Nothing here is on the model's path: `ledger query` runs raw SQL precisely
because it is *not* a product surface, and the tool layer remains the only way
the model reaches the data.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pytest

from ledger import cli
from ledger.config import Settings, get_settings
from ledger.errors import LedgerError


@pytest.fixture
def cli_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Point the CLI at the fixture dataset rather than the real download."""
    monkeypatch.setenv("LEDGER_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("LEDGER_MONTHS", ",".join(settings.months))
    monkeypatch.setenv("LEDGER_CATALOG_MODE", "offline")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_no_arguments_prints_help_rather_than_failing() -> None:
    """A bare `ledger` should teach, not exit non-zero with nothing said."""
    assert cli.main([]) == 0


def test_version_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_code:
        cli.main(["--version"])
    assert exit_code.value.code == 0
    assert "ledger" in capsys.readouterr().out


def test_info_reports_the_resolved_configuration(
    cli_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fastest way to see why a run behaved as it did.

    It reports the *resolved* backend, not the configured one, because `auto`
    is the default and the difference between "auto" and what auto became is
    the whole question being asked.
    """
    assert cli.main(["info"]) == 0
    out = capsys.readouterr().out
    assert "version" in out
    assert "model backend" in out
    assert "->" in out, "the resolved backend is what the operator needs"
    assert "kafka" in out


def test_query_prints_a_table(cli_settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["query", "select 1 as one, 'x' as two"]) == 0
    out = capsys.readouterr().out
    assert "one" in out and "two" in out
    assert "---" in out, "a header rule separates the columns from the rows"
    assert "1" in out


def test_query_truncates_and_says_so(
    cli_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silently printing 50 of 200 rows is how a wrong conclusion gets drawn."""
    assert cli.main(["query", "select * from range(200)", "--limit", "5"]) == 0
    out = capsys.readouterr().out
    assert "195 more row(s)" in out


def test_query_against_the_normalised_view(
    cli_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["query", "select count(*) as n from ledger.trips"]) == 0
    assert "45000" in capsys.readouterr().out.replace(",", "")


def test_a_broken_query_is_reported_not_raised(cli_settings: Settings) -> None:
    """An operator gets a message and an exit code, not a traceback."""
    with pytest.raises(Exception) as raised:
        cli.main(["query", "select * from no_such_table"])
    assert "no_such_table" in str(raised.value)


def test_a_ledger_error_becomes_an_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator gets a message and exit 1, not a traceback.

    `LedgerError` is the class carrying a message meant to be read by a person,
    so the CLI catches exactly that and lets anything else surface -- an
    unexpected exception is a bug, and hiding it behind exit 1 costs the one
    thing that would have identified it.
    """

    def unreachable(_: argparse.Namespace) -> int:
        raise LedgerError("the broker is unreachable")

    monkeypatch.setattr(cli, "_cmd_info", unreachable)
    assert cli.main(["info"]) == 1


def test_an_unexpected_exception_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def bug(_: argparse.Namespace) -> int:
        raise ZeroDivisionError("a real bug")

    monkeypatch.setattr(cli, "_cmd_info", bug)
    with pytest.raises(ZeroDivisionError):
        cli.main(["info"])


def test_catalog_show_renders_what_a_role_would_see(
    cli_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same render the prompt gets, which is why it is worth a command."""
    assert cli.main(["catalog", "show", "--role", "analyst"]) == 0
    analyst = capsys.readouterr().out

    assert cli.main(["catalog", "show", "--role", "viewer"]) == 0
    viewer = capsys.readouterr().out

    assert "trip_distance" in analyst and "trip_distance" in viewer
    assert "tip_amount" in analyst
    assert "tip_amount" not in viewer, "the CLI render must be scoped like every other surface"
    assert len(viewer) < len(analyst)


def test_an_unknown_role_is_refused_by_the_parser(cli_settings: Settings) -> None:
    with pytest.raises(SystemExit):
        cli.main(["catalog", "show", "--role", "root"])


def test_catalog_build_writes_a_catalogue(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Profiling only -- `--enrich` is what costs money, and it is not passed.

    Written into a copy of the fixture tree rather than the fixture itself.
    The catalogue path is derived from the data directory, so pointing the
    command at the committed fixture overwrites a tracked file, which a test
    has no business doing.
    """
    shutil.copytree(settings.data_dir, tmp_path / "data")
    monkeypatch.setenv("LEDGER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LEDGER_MONTHS", ",".join(settings.months))
    get_settings.cache_clear()

    assert cli.main(["catalog", "build"]) == 0
    written = tmp_path / "data" / "catalog" / "catalog.json"
    assert written.exists()
    assert "columns" in written.read_text()
