"""The golden-question regression suite.

Run offline against the scripted model in CI, and against the real model under
``-m ai_live``. When a prompt or a tool description changes, this is what tells
you what it broke.
"""

from __future__ import annotations

from typing import Any

import pytest

from ledger.tools.context import ToolContext
from tests.conftest import published
from tests.golden import harness

QUESTIONS = harness.load_questions()

pytestmark = pytest.mark.golden


@pytest.mark.parametrize("question", QUESTIONS, ids=[q["id"] for q in QUESTIONS])
async def test_golden_question(
    question: dict[str, Any],
    analyst_ctx: ToolContext,
    viewer_ctx: ToolContext,
) -> None:
    ctx = analyst_ctx if question["role"] == "analyst" else viewer_ctx
    model = harness.model_for(question)

    trace = await harness.run(ctx, model, question["question"])
    problems = harness.check(question, trace, published(ctx).denied_columns())

    if problems:
        detail = "\n  - ".join(problems)
        pytest.fail(
            f"{question['id']} ({question['role']}): {question['question']}\n"
            f"  - {detail}\n\n"
            f"  answer: {trace.answer[:300]!r}\n"
            f"  calls: {[(c.tool, 'ok' if c.ok else c.error) for c in trace.calls]}",
            pytrace=False,
        )


def test_the_suite_covers_both_roles_and_the_failure_paths() -> None:
    """A golden suite of only happy paths is a smoke test wearing a costume."""
    roles = {q["role"] for q in QUESTIONS}
    assert roles == {"analyst", "viewer"}

    expected_errors = {
        step.get("error")
        for q in QUESTIONS
        for step in q["expect"].get("tool_sequence", [])
        if step.get("ok") is False
    }
    # Recovery from a hallucinated column, a refused group-by, and an RBAC
    # refusal are all regression-tested, not just the paths that work.
    assert {"unknown_column", "cardinality_exceeded"} <= expected_errors

    assert any(q["expect"].get("answer", {}).get("cites_result") for q in QUESTIONS)
    assert any(q["expect"].get("denied_columns") for q in QUESTIONS)
