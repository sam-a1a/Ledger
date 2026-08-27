"""`ledger doctor` -- the command that exists so a misconfiguration is legible.

Its whole value is being right about what is wrong, so each check is asserted
against a deliberately broken configuration rather than only a healthy one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ledger.config import DEV_JWT_SECRET, AuthMode, CatalogMode, Settings
from ledger.doctor import (
    Status,
    _check_auth,
    _check_catalog,
    _check_data,
    _check_model,
    report,
)


def test_a_missing_dataset_says_how_to_get_one(tmp_path: Path) -> None:
    finding = _check_data(Settings(data_dir=tmp_path))
    assert finding.status is Status.FAIL
    assert finding.fix and "make fetch" in finding.fix


def test_a_present_dataset_reports_its_size(settings: Settings) -> None:
    finding = _check_data(settings)
    assert finding.status is Status.OK
    assert "file(s)" in finding.detail


def test_a_missing_catalogue_is_fatal_only_in_offline_mode(tmp_path: Path) -> None:
    """`offline` promises never to build one, so its absence is unrecoverable."""
    auto = _check_catalog(Settings(data_dir=tmp_path, catalog_mode=CatalogMode.AUTO))
    offline = _check_catalog(Settings(data_dir=tmp_path, catalog_mode=CatalogMode.OFFLINE))
    assert auto.status is Status.WARN
    assert offline.status is Status.FAIL
    assert offline.fix == "ledger catalog build"


def test_no_api_key_is_a_warning_not_a_failure() -> None:
    """Running scripted is a supported mode, not a broken one."""
    finding = _check_model(Settings(anthropic_api_key=None))
    assert finding.status is Status.WARN
    assert "scripted" in finding.detail
    assert finding.fix and "platform.claude.com" in finding.fix


def test_asking_for_the_real_model_without_a_key_is_a_failure() -> None:
    """The regression this test exists for.

    `resolved_backend()` honours an explicit LEDGER_MODEL even with no key, so
    checking the happy case first reported a broken configuration as fine.
    """
    finding = _check_model(Settings(model_backend="anthropic", anthropic_api_key=None))
    assert finding.status is Status.FAIL
    assert "ANTHROPIC_API_KEY" in finding.detail


def test_a_configured_key_is_reported_without_leaking_it() -> None:
    finding = _check_model(
        Settings(model_backend="anthropic", anthropic_api_key="sk-ant-secret-tail1234")
    )
    assert finding.status is Status.OK
    assert "sk-ant-secret" not in finding.detail
    assert "1234" in finding.detail  # enough to identify, not to use


def test_the_development_key_warns_locally_and_fails_in_strict_mode() -> None:
    local = _check_auth(Settings(auth_mode=AuthMode.DEV, jwt_secret=DEV_JWT_SECRET))
    strict = _check_auth(Settings(auth_mode=AuthMode.STRICT, jwt_secret=DEV_JWT_SECRET))
    assert local.status is Status.WARN
    assert strict.status is Status.FAIL


def test_a_real_signing_key_passes() -> None:
    finding = _check_auth(Settings(jwt_secret="a-genuinely-configured-signing-key-of-length"))
    assert finding.status is Status.OK


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_signing_key_falls_back_rather_than_breaking_every_login(blank: str) -> None:
    """`.env` and container environments both express "unset" as an empty string.

    Taking that literally is how an empty key silently replaced a working
    default and made every login fail with "HMAC key must not be empty".
    """
    assert Settings(jwt_secret=blank).jwt_secret == DEV_JWT_SECRET
    assert _check_auth(Settings(jwt_secret=blank)).status is Status.WARN


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_api_key_means_unset(blank: str) -> None:
    settings = Settings(anthropic_api_key=blank)
    assert settings.anthropic_api_key is None
    assert settings.demo_mode


def test_the_report_names_the_fix_for_anything_broken(tmp_path: Path) -> None:
    findings = [_check_data(Settings(data_dir=tmp_path))]
    rendered = report(findings)
    assert "FAIL" in rendered
    assert "->" in rendered  # the remedy, not just the diagnosis


def test_the_report_does_not_nag_about_healthy_checks(settings: Settings) -> None:
    rendered = report([_check_data(settings)])
    assert "->" not in rendered
