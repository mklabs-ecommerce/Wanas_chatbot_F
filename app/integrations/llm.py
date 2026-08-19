"""LLM entrypoint: one ``generate()`` call, with a resilience chain behind it.

Answers the rate-limit question from Section 9 of the build plan. The chain is:

1. the configured primary Gemini model, retried with backoff on 503s and timeouts
2. each configured Gemini fallback model in turn

Every Gemini model carries its own free-tier request budget (measured at ~20 per rolling
window per model on 2026-08-17), so spanning several models multiplies the allowance. A
model that returns 429 is put in cooldown for exactly the delay the API asks for and
skipped until then, rather than re-probed on every turn.

When the chain is exhausted, ``LLMAllProvidersFailed`` reaches the caller, which turns it
into a polite apology. That is deliberate: an honest "I am busy, try again shortly" is
better than an answer from a model that cannot be trusted with the customer's question.

A third-party fallback (OpenRouter's free Gemma) was built and then removed - it answered
catalog questions without calling the search tool and asserted stock levels that were
false, roughly once in five questions. See the README for the detail.

This stays in ``integrations`` because it is about reaching an API reliably, not about
what to say. Callers (``modules/chat``) supply the prompt, the history and the tools.
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.config import settings
from app.integrations.gemini.client import GeminiClient
from app.integrations.llm_types import (
    LLMAllProvidersFailed,
    LLMError,
    LLMRateLimited,
    LLMResponse,
    Turn,
)

logger = logging.getLogger(__name__)

PROVIDER = "gemini"

_client: Optional[GeminiClient] = None

# Models known to be out of quota, mapped to the monotonic time they may be tried again.
# Without this, every turn would re-probe an exhausted model and pay its latency first.
_cooldowns: Dict[Tuple[str, str], float] = {}

# Default pause when the provider does not say how long to wait.
DEFAULT_COOLDOWN_SECONDS = 60.0
MAX_COOLDOWN_SECONDS = 900.0

# Gemini phrases its quota errors as "Please retry in 30.5s" and also includes a
# RetryInfo block with "retryDelay: '30s'"; either form is accepted.
_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry in ([\d.]+)s", re.IGNORECASE),
    re.compile(r"retryDelay['\"]?:\s*['\"]?(\d+)s", re.IGNORECASE),
)


def _gemini_client() -> Optional[GeminiClient]:
    """Lazily built so importing this module never requires a configured key."""
    global _client
    if _client is None and settings.gemini_configured:
        _client = GeminiClient()
    return _client


def _retry_after_seconds(message: str) -> float:
    """Pull the provider's suggested wait out of an error message."""
    for pattern in _RETRY_AFTER_PATTERNS:
        found = pattern.search(message)
        if found:
            try:
                return min(float(found.group(1)), MAX_COOLDOWN_SECONDS)
            except ValueError:
                pass
    return DEFAULT_COOLDOWN_SECONDS


def _cooling_down(key: Tuple[str, str]) -> bool:
    """True while this provider/model is still inside its cooldown window."""
    until = _cooldowns.get(key)
    if until is None:
        return False
    if time.monotonic() >= until:
        del _cooldowns[key]
        return False
    return True


def _start_cooldown(key: Tuple[str, str], message: str) -> None:
    seconds = _retry_after_seconds(message)
    _cooldowns[key] = time.monotonic() + seconds
    logger.warning("Skipping %s/%s for the next %.0fs (out of quota)", key[0], key[1], seconds)


def reset_cooldowns() -> None:
    """Clear all cooldowns. Used by tests and available for a manual reset."""
    _cooldowns.clear()


def models() -> List[str]:
    """The ordered list of models to try."""
    if not settings.gemini_configured:
        return []
    return [settings.gemini_model] + list(settings.gemini_fallback_models)


async def generate(
    *,
    turns: Sequence[Turn],
    system_instruction: Optional[str] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    temperature: Optional[float] = None,
    json_output: bool = False,
    model: Optional[str] = None,
) -> LLMResponse:
    """Generate one reply, walking the model chain until something answers.

    ``model`` pins the request to one model instead of the configured chain - used where
    a task needs a particular model rather than whichever is available.
    """
    client = _gemini_client()
    chain = [model] if model else models()
    if client is None or not chain:
        raise LLMAllProvidersFailed("No LLM provider is configured")

    errors: List[str] = []
    attempted = 0
    for index, candidate in enumerate(chain):
        key = (PROVIDER, candidate)
        if _cooling_down(key):
            errors.append(candidate + ": skipped, cooling down after a quota error")
            continue

        attempted += 1
        try:
            response = await client.generate(
                turns=turns,
                model=candidate,
                system_instruction=system_instruction,
                tools=tools,
                temperature=temperature,
                json_output=json_output,
            )
        except LLMRateLimited as exc:
            # Quota errors are remembered, so later turns skip this model immediately.
            _start_cooldown(key, str(exc))
            errors.append(candidate + ": " + str(exc))
            continue
        except LLMError as exc:
            errors.append(candidate + ": " + str(exc))
            logger.warning("LLM attempt %d/%d failed (%s): %s",
                           index + 1, len(chain), candidate, exc)
            continue

        if index > 0:
            # Anything past the primary is a degraded answer; surfaced in the API
            # response so the widget and the logs can show it during testing.
            response.degraded = True
            logger.warning("Answered via fallback %s after %d skipped/failed model(s)",
                           candidate, index)
        return response

    raise LLMAllProvidersFailed("; ".join(errors))
