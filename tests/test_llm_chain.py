"""The LLM resilience chain: retry, model fallback, quota cooldowns, then apology.

This is the implementation of Section 9's rate-limit question, so it is tested with fake
providers rather than real ones - the failure paths must be verifiable without waiting
for a genuine 429.
"""

from typing import List

import pytest

from app.integrations import llm
from app.integrations.llm_types import (
    LLMAllProvidersFailed,
    LLMRateLimited,
    LLMResponse,
    LLMUnavailable,
    Turn,
)


class FakeClient:
    """Records the models it was asked for, and fails or answers as scripted."""

    def __init__(self, script):
        self.script = script
        self.calls: List[str] = []

    async def generate(self, *, turns, model, system_instruction=None, tools=None,
                       temperature=None, json_output=False):
        self.calls.append(model)
        outcome = self.script.get(model, LLMUnavailable("unscripted model " + model))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def chain(monkeypatch):
    """Configure a two-model chain: a primary and one fallback."""
    monkeypatch.setattr(llm.settings, "gemini_api_key", "k", raising=False)
    monkeypatch.setattr(llm.settings, "gemini_model", "primary", raising=False)
    monkeypatch.setattr(llm.settings, "gemini_fallback_models", ["secondary"], raising=False)
    return monkeypatch


def _install(monkeypatch, gemini_script):
    gemini = FakeClient(gemini_script)
    monkeypatch.setattr(llm, "_gemini_client", lambda: gemini)
    return gemini


TURNS = [Turn(role="user", text="hello")]


async def test_primary_answers_and_is_not_marked_degraded(chain):
    gemini = _install(
        chain,
        {"primary": LLMResponse(text="hi", model="primary", provider="gemini")},
    )
    response = await llm.generate(turns=TURNS)

    assert response.text == "hi"
    assert response.degraded is False
    assert gemini.calls == ["primary"]


async def test_rate_limited_primary_falls_back_to_second_gemini_model(chain):
    gemini = _install(
        chain,
        {
            "primary": LLMRateLimited("429 quota exhausted"),
            "secondary": LLMResponse(text="from secondary", model="secondary",
                                     provider="gemini"),
        },
    )
    response = await llm.generate(turns=TURNS)

    assert response.text == "from secondary"
    # Anything past the primary is flagged, so the widget and logs show it.
    assert response.degraded is True
    assert gemini.calls == ["primary", "secondary"]


async def test_every_model_failing_raises_with_all_causes(chain):
    """There is no third-party fallback by design: an honest apology beats a bad answer.

    A free OpenRouter model was tried and removed - it answered catalog questions without
    calling the search tool and stated stock levels that were false.
    """
    _install(
        chain,
        {"primary": LLMRateLimited("429 gemini"), "secondary": LLMUnavailable("503 gemini")},
    )
    with pytest.raises(LLMAllProvidersFailed) as raised:
        await llm.generate(turns=TURNS)

    # Every cause is reported so the log explains what actually happened.
    message = str(raised.value)
    assert "429 gemini" in message
    assert "503 gemini" in message


async def test_no_provider_configured_is_reported_not_silently_empty(monkeypatch):
    monkeypatch.setattr(llm.settings, "gemini_api_key", "", raising=False)
    with pytest.raises(LLMAllProvidersFailed):
        await llm.generate(turns=TURNS)


# --- quota cooldowns -----------------------------------------------------


async def test_quota_error_puts_the_model_in_cooldown_and_later_turns_skip_it(chain):
    """A model that is out of quota must not be re-probed on every following turn."""
    gemini = _install(
        chain,
        {
            "primary": LLMRateLimited("429 quota exceeded. Please retry in 30.5s"),
            "secondary": LLMResponse(text="ok", model="secondary", provider="gemini"),
        },
    )
    await llm.generate(turns=TURNS)
    assert gemini.calls == ["primary", "secondary"]

    # Second turn: the primary is skipped outright, so only the secondary is called.
    await llm.generate(turns=TURNS)
    assert gemini.calls == ["primary", "secondary", "secondary"]


async def test_cooldown_uses_the_delay_the_provider_asked_for(chain):
    _install(chain, {"primary": LLMRateLimited("Please retry in 45s")})
    llm._start_cooldown(("gemini", "primary"), "Please retry in 45s")

    remaining = llm._cooldowns[("gemini", "primary")] - __import__("time").monotonic()
    assert 40 < remaining <= 45


async def test_cooldown_expires_and_the_model_is_tried_again(chain, monkeypatch):
    gemini = _install(
        chain,
        {
            "primary": LLMResponse(text="primary is back", model="primary", provider="gemini"),
            "secondary": LLMResponse(text="ok", model="secondary", provider="gemini"),
        },
    )
    # A cooldown that has already elapsed must be cleared, not honoured forever.
    llm._cooldowns[("gemini", "primary")] = __import__("time").monotonic() - 1

    response = await llm.generate(turns=TURNS)
    assert response.text == "primary is back"
    assert response.degraded is False


async def test_a_cooled_down_primary_still_reports_the_answer_as_degraded(chain):
    _install(
        chain,
        {"secondary": LLMResponse(text="ok", model="secondary", provider="gemini")},
    )
    llm._cooldowns[("gemini", "primary")] = __import__("time").monotonic() + 300

    response = await llm.generate(turns=TURNS)
    assert response.text == "ok"
    assert response.degraded is True


async def test_cooldown_reasons_appear_in_the_final_error(chain):
    _install(chain, {})
    now = __import__("time").monotonic()
    for model in ("primary", "secondary"):
        llm._cooldowns[("gemini", model)] = now + 300

    with pytest.raises(LLMAllProvidersFailed) as raised:
        await llm.generate(turns=TURNS)
    assert "cooling down" in str(raised.value)


# --- pinning one model ---------------------------------------------------


async def test_a_pinned_model_is_used_instead_of_the_chain(chain):
    """Image matching and conversation are different jobs, and can want different models."""
    gemini = _install(
        chain,
        {"primary": LLMResponse(text="from primary", model="primary", provider="gemini"),
         "vision": LLMResponse(text="from vision", model="vision", provider="gemini")},
    )
    response = await llm.generate(turns=TURNS, model="vision")

    assert response.text == "from vision"
    assert gemini.calls == ["vision"]


async def test_a_pinned_model_does_not_fall_back_to_the_chain(chain):
    """Silently answering from a different model would defeat the point of pinning."""
    _install(chain, {"primary": LLMResponse(text="hi", model="primary", provider="gemini")})

    with pytest.raises(LLMAllProvidersFailed):
        await llm.generate(turns=TURNS, model="vision")


async def test_a_pinned_model_still_honours_its_cooldown(chain):
    _install(chain, {"vision": LLMResponse(text="ok", model="vision", provider="gemini")})
    llm._cooldowns[("gemini", "vision")] = __import__("time").monotonic() + 300

    with pytest.raises(LLMAllProvidersFailed):
        await llm.generate(turns=TURNS, model="vision")
