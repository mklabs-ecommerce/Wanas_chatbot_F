"""Raw Gemini API client.

A dumb wrapper: it knows how to phrase a request for Google's SDK, how to normalise the
reply, and how to retry when Google is briefly unwilling. It holds no prompts, no tool
definitions and no business rules — those live in ``modules/chat`` and the other modules.
"""

import asyncio
import logging
import random
import re
from typing import Any, Dict, List, Optional, Sequence

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app.core.config import settings
from app.integrations.llm_types import (
    ImagePart,
    LLMRateLimited,
    LLMResponse,
    LLMUnavailable,
    ToolCall,
    Turn,
)

logger = logging.getLogger(__name__)

PROVIDER = "gemini"

# Google's SDK raises ClientError for 4xx and ServerError for 5xx. Only these are worth
# another attempt on the *same* model. 429 is deliberately absent: quota errors carry a
# retry delay measured in tens of seconds, so retrying immediately just burns time -
# the caller moves to the next model in the chain instead.
_RETRYABLE_STATUS = {408, 500, 502, 503, 504}


class GeminiClient:
    """Thin async wrapper over ``google-genai``."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        key = api_key if api_key is not None else settings.gemini_api_key
        if not key:
            raise ValueError("GEMINI_API_KEY is not configured")
        self._client = genai.Client(api_key=key)

    # -- public API ------------------------------------------------------
    async def generate(
        self,
        *,
        turns: Sequence[Turn],
        model: str,
        system_instruction: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
        json_output: bool = False,
        max_attempts: int = 3,
    ) -> LLMResponse:
        """Generate one reply.

        Retries the same model on transient failures with jittered backoff. Choosing a
        *different* model or provider is the caller's job (see ``integrations/llm.py``).
        """
        contents = [_to_content(turn) for turn in turns]
        config = self._build_config(model, system_instruction, tools, temperature,
                                    json_output)

        last_error: Exception = LLMUnavailable("no attempt was made")
        for attempt in range(1, max_attempts + 1):
            try:
                response = await self._client.aio.models.generate_content(
                    model=model, contents=contents, config=config
                )
                return _to_llm_response(response, model)
            except (LLMRateLimited, LLMUnavailable) as exc:
                last_error = exc
                retryable = True
            except genai_errors.APIError as exc:
                status = getattr(exc, "code", None)
                if status == 429:
                    # Out of quota on this model; hand straight back so the chain can try
                    # a different one rather than waiting out a 30s+ retry delay.
                    logger.warning("Gemini %s is out of quota: %s", model, exc)
                    raise LLMRateLimited(str(exc)) from exc
                last_error = LLMUnavailable(str(exc))
                if status not in _RETRYABLE_STATUS:
                    logger.warning("Gemini %s returned a non-retryable error: %s", model, exc)
                    raise last_error from exc
            except asyncio.TimeoutError as exc:
                last_error = LLMUnavailable(f"timed out: {exc}")
                retryable = True

            if attempt == max_attempts:
                break
            delay = min(8.0, 0.6 * (2 ** (attempt - 1))) + random.uniform(0, 0.4)
            logger.warning(
                "Gemini %s attempt %d/%d failed (%s); retrying in %.1fs",
                model, attempt, max_attempts, last_error, delay,
            )
            await asyncio.sleep(delay)

        raise last_error

    # -- internals -------------------------------------------------------
    def _build_config(
        self,
        model: str,
        system_instruction: Optional[str],
        tools: Optional[List[Dict[str, Any]]],
        temperature: Optional[float],
        json_output: bool = False,
    ) -> types.GenerateContentConfig:
        kwargs: Dict[str, Any] = {
            "system_instruction": system_instruction,
            "temperature": settings.gemini_temperature if temperature is None else temperature,
            # Always off. Tool calls are dispatched by hand in modules/chat so every one
            # passes through a module service; the SDK must never invoke anything itself.
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }
        if _supports_thinking_level(model):
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=settings.gemini_thinking_level
            )
        if tools:
            kwargs["tools"] = [types.Tool(function_declarations=tools)]
        elif json_output:
            # Only without tools: a model cannot be asked to answer in JSON and to call
            # functions in the same request.
            kwargs["response_mime_type"] = "application/json"
        return types.GenerateContentConfig(**kwargs)


def _supports_thinking_level(model: str) -> bool:
    """Only the 3.x family accepts ``thinking_level``.

    2.5 models expose ``thinking_budget`` instead and reject the newer field outright
    with "Thinking level is not supported for this model", which would knock every 2.5
    fallback out of the chain.
    """
    return bool(re.match(r"gemini-3(\.|-)", model))


def _to_content(turn: Turn) -> types.Content:
    """Translate one neutral ``Turn`` into Gemini's ``Content`` shape."""
    parts: List[types.Part] = []
    if turn.text:
        parts.append(types.Part.from_text(text=turn.text))
    for image in turn.images:
        parts.append(_image_part(image))
    for call in turn.tool_calls:
        part = types.Part.from_function_call(name=call.name, args=call.arguments)
        if call.signature:
            # Required by Gemini 3.x: replaying a function call without its signature
            # fails with "Function call is missing a thought_signature".
            part.thought_signature = call.signature
        parts.append(part)
    for result in turn.tool_results:
        parts.append(
            types.Part.from_function_response(name=result.name, response=result.result)
        )
    if not parts:
        parts.append(types.Part.from_text(text=""))
    return types.Content(role=turn.role, parts=parts)


def _image_part(image: ImagePart) -> types.Part:
    return types.Part.from_bytes(data=image.data, mime_type=image.mime_type)


def _to_llm_response(response: Any, model: str) -> LLMResponse:
    """Normalise a Gemini response, treating an empty candidate list as unavailability."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        raise LLMUnavailable("Gemini returned no candidates")

    parts = getattr(getattr(candidates[0], "content", None), "parts", None) or []
    texts: List[str] = []
    tool_calls: List[ToolCall] = []
    for part in parts:
        # Skip the model's internal reasoning; only user-facing text is wanted.
        if getattr(part, "thought", False):
            continue
        if getattr(part, "text", None):
            texts.append(part.text)
        call = getattr(part, "function_call", None)
        if call is not None:
            tool_calls.append(
                ToolCall(
                    name=call.name,
                    arguments=dict(call.args or {}),
                    id=getattr(call, "id", None),
                    signature=getattr(part, "thought_signature", None),
                )
            )

    text = "".join(texts).strip()
    if not text and not tool_calls:
        finish = getattr(candidates[0], "finish_reason", None)
        raise LLMUnavailable(f"Gemini returned an empty reply (finish_reason={finish})")

    return LLMResponse(text=text, tool_calls=tool_calls, model=model, provider=PROVIDER)
