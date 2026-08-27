"""Kafka is a hard dependency, and that decision is enforced rather than intended.

If someone later adds a ``NullAuditPublisher`` "just for local dev", the
governance claim quietly becomes decorative and nothing else in the suite would
notice. This test notices. It needs no broker.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re

import ledger
from ledger.governance import publisher as publisher_module
from ledger.governance.publisher import AuditPublisher, KafkaAuditPublisher

DISABLED_NAME = re.compile(r"(null|noop|no_op|disabled|fake|dummy|stub)", re.IGNORECASE)


def _concrete_publishers(module: object) -> list[type]:
    return [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if obj is not AuditPublisher
        and hasattr(obj, "publish")
        and obj.__module__.startswith("ledger.")
    ]


def test_exactly_one_publisher_implementation_ships() -> None:
    assert _concrete_publishers(publisher_module) == [KafkaAuditPublisher]


def test_no_module_under_src_defines_a_disabled_publisher() -> None:
    """Walk the whole application package, not just the one module."""
    offenders: list[str] = []
    # Deliberately not guarded: a module under `ledger.` that cannot be imported
    # is itself worth failing on, and swallowing the error here would let an
    # offender hide behind a broken import.
    for info in pkgutil.walk_packages(ledger.__path__, prefix="ledger."):
        module = importlib.import_module(info.name)
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if not obj.__module__.startswith("ledger."):
                continue
            if hasattr(obj, "publish") and DISABLED_NAME.search(name):
                offenders.append(f"{obj.__module__}.{name}")
    assert not offenders, (
        "Kafka is a hard dependency: no disabled/null audit publisher may ship "
        f"in the application. Found: {sorted(set(offenders))}"
    )


def test_kafka_publisher_satisfies_the_protocol() -> None:
    assert issubclass(KafkaAuditPublisher, AuditPublisher)


def test_settings_expose_no_switch_to_turn_auditing_off() -> None:
    """There must be no `audit_enabled`-shaped knob."""
    from ledger.config import Settings

    suspicious = [
        name
        for name in Settings.model_fields
        if re.search(r"(audit|governance|kafka).*(enabled|disabled|optional|off)", name, re.I)
    ]
    assert not suspicious, f"found a switch that could disable auditing: {suspicious}"
