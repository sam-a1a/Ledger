"""Choosing a model backend.

Auto-detects when unset, and **fails loudly** when the real backend is asked
for without a key. A silent fall back to the fake there would be the wrong
answer: it would mask a misconfigured deployment behind plausible-looking
scripted answers, which is far worse than refusing to start.
"""

from __future__ import annotations

from ledger.config import ModelBackend, Settings
from ledger.errors import ConfigurationError
from ledger.logging import get_logger
from ledger.model.fake import FakeModelClient, Responder
from ledger.model.protocol import ModelClient

log = get_logger(__name__)


def make_model_client(
    settings: Settings, *, fake_responder: Responder | None = None
) -> ModelClient:
    backend = settings.resolved_backend()

    if backend is ModelBackend.ANTHROPIC:
        if not settings.anthropic_api_key:
            raise ConfigurationError(
                "LEDGER_MODEL=anthropic but ANTHROPIC_API_KEY is unset. Unset "
                "LEDGER_MODEL to fall back to the scripted model deliberately."
            )
        from ledger.model.anthropic_client import AnthropicModelClient

        log.info("model backend: anthropic (%s)", settings.anthropic_model)
        return AnthropicModelClient(settings)

    if fake_responder is None:
        from ledger.model.scripts import default_responder

        fake_responder = default_responder()

    log.warning(
        "model backend: fake -- answers are scripted, not generated. "
        "Set ANTHROPIC_API_KEY to use %s.",
        settings.anthropic_model,
    )
    return FakeModelClient(fake_responder, token_delay_ms=settings.fake_token_delay_ms)
