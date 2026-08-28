"""Per-module coverage floors, and the table CI publishes.

A single overall floor lets an average hide a module that is barely tested:
the total was comfortably over 80% while `avatars.py` -- an upload handler,
the most commonly exploited surface in a web application -- sat at 23%.

So each module gets a floor of its own. Most sit at the project default. The
exceptions are listed with a reason, and the reason has to be something other
than "it is hard": a module that is hard to test is usually a module that is
hard to be sure about.

Floors are a ratchet, not a target. Raising one after improving a module is
the point; lowering one needs a reason written down here next to it.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_FLOOR = 75.0

#: Module path as it appears in `coverage.xml`, which is relative to the source
#: root: `api/app.py`, not `src/ledger/api/app.py`.
#:
#: module path -> (floor, why it is not the default)
EXCEPTIONS: dict[str, tuple[float, str]] = {
    "model/anthropic_client.py": (
        30.0,
        "the live model path; exercised by the nightly ai_live job, which needs a key",
    ),
    "governance/consumer.py": (
        60.0,
        "the long-running drain loop is covered end-to-end by the Playwright audit spec, "
        "which runs a real consumer, rather than by unit tests of the loop itself",
    ),
    "mcp_server/server.py": (
        50.0,
        "thin wrappers whose schemas are asserted by the parity tests; the executor "
        "behind them is covered directly, and driving stdio in-process would test the "
        "harness rather than the server",
    ),
    "api/routes/health.py": (
        50.0,
        "readiness probes each check a dependency; asserting them needs the dependency "
        "broken in a specific way, which the deployment smoke job does for real",
    ),
    "doctor.py": (
        60.0,
        "reports on a misconfigured environment, so most branches need a broken one; "
        "the branches that matter are covered in tests/unit/test_doctor.py",
    ),
    "api/app.py": (
        60.0,
        "startup wiring; what it builds is covered through the API tests, and the "
        "failure paths need a broken broker or database",
    ),
    "model/factory.py": (
        60.0,
        "resolves a backend from configuration; the anthropic branch needs a key",
    ),
}


def main() -> int:
    report = Path("coverage.xml")
    if not report.exists():
        sys.stderr.write("coverage.xml not found; run pytest --cov --cov-report=xml first\n")
        return 2

    # Our own build artefact, written by coverage.py moments earlier -- not
    # untrusted input, which is what the rule is about.
    root = ET.parse(report).getroot()  # noqa: S314
    rows: list[tuple[str, float, float, str]] = []
    for module in root.iter("class"):
        path = module.get("filename") or ""
        if not path.endswith(".py"):
            continue
        rate = float(module.get("line-rate") or 0.0) * 100.0
        floor, reason = EXCEPTIONS.get(path, (DEFAULT_FLOOR, ""))
        rows.append((path, rate, floor, reason))

    failures = [row for row in sorted(rows) if row[1] < row[2]]

    print("| module | coverage | floor | |")
    print("| --- | ---: | ---: | :-- |")
    for path, rate, floor, reason in sorted(rows, key=lambda r: r[1]):
        mark = "❌" if rate < floor else ""
        note = f" {reason}" if reason else ""
        print(f"| `{path}` | {rate:.0f}% | {floor:.0f}% | {mark}{note} |")

    if failures:
        print()
        for path, rate, floor, _ in failures:
            print(f"{path} is at {rate:.0f}%, below its floor of {floor:.0f}%")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
