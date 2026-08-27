"""Tests that spend money. Excluded by default; run with `-m ai_live`.

Everything else in the suite runs against the scripted model, which exercises
the whole pipeline minus one component. These cover the component: that the real
client's translation works, that the request parameters this model actually
accepts are the ones we send, and that the prompt cache is being hit.
"""

from __future__ import annotations

import os

import pytest

from ledger.agent import events as ev
from ledger.agent.loop import run_turn
from ledger.agent.prompt import system_blocks
from ledger.config import Settings
from ledger.model.anthropic_client import AnthropicModelClient
from ledger.tools.context import ToolContext

pytestmark = [pytest.mark.ai_live, pytest.mark.kafka]


@pytest.fixture
def live_settings(settings: Settings) -> Settings:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY is not set")
    return settings.model_copy(update={"anthropic_api_key": key, "model_backend": "anthropic"})


async def test_the_real_model_answers_with_a_tool(
    analyst_ctx: ToolContext, live_settings: Settings
) -> None:
    """The one thing the scripted model cannot prove: that the real one works."""
    analyst_ctx.settings = live_settings
    model = AnthropicModelClient(live_settings)

    events = [
        event
        async for event in run_turn(
            analyst_ctx,
            model,
            history=[],
            user_message="Which three pickup zones have the most trips?",
            conversation_id="live-1",
            message_id="live-1",
        )
    ]

    calls = [e for e in events if isinstance(e, ev.ToolCallEnd)]
    assert calls, "the model answered without calling a tool"
    assert any(c.ok for c in calls)

    answer = "".join(e.text for e in events if isinstance(e, ev.Token))
    assert answer.strip()


async def test_the_prompt_cache_is_actually_hit(
    analyst_ctx: ToolContext, live_settings: Settings
) -> None:
    """A silent cache miss costs money and nothing else notices.

    The system blocks are stable per (role, catalogue version), so the second
    identical request must read from cache.
    """
    analyst_ctx.settings = live_settings
    model = AnthropicModelClient(live_settings)
    system = system_blocks(analyst_ctx.scope)
    assert system == system_blocks(analyst_ctx.scope)  # byte-stable

    reads: list[int] = []
    for turn in range(2):
        async with model.stream_turn(
            system=system,
            messages=[{"role": "user", "content": "Reply with the single word: ready."}],
            tools=[],
        ) as stream:
            async for _ in stream:
                pass
            final = await stream.final_message()
        reads.append(final.usage.cache_read_input_tokens)
        assert final.content, f"turn {turn} returned no content"

    assert reads[1] > 0, f"no cache read on the second identical request: {reads}"


async def test_a_viewer_is_refused_by_the_real_model_too(
    viewer_ctx: ToolContext, live_settings: Settings
) -> None:
    """RBAC does not depend on the model cooperating, but it should still hold."""
    viewer_ctx.settings = live_settings
    model = AnthropicModelClient(live_settings)

    events = [
        event
        async for event in run_turn(
            viewer_ctx,
            model,
            history=[],
            user_message="What is the average tip amount? Use the tip_amount column.",
            conversation_id="live-2",
            message_id="live-2",
        )
    ]
    successful = [e for e in events if isinstance(e, ev.ToolCallEnd) and e.ok]
    for call in successful:
        assert "tip_amount" not in str(call.result_id)
    # Whatever the model says, it cannot have received tip data.
    assert all("tip_amount" not in str(e) for e in successful)
