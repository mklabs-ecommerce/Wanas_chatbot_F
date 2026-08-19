"""Translation between our neutral ``Turn`` shape and Gemini's dialect.

No network: only the pure request-building and response-parsing helpers are exercised.
The tool-call paths matter most - a dropped thought signature or a mishandled function
response breaks every catalog answer.
"""

import pytest

from app.integrations.gemini import client as gemini_client
from app.integrations.llm_types import (
    ImagePart,
    LLMUnavailable,
    ToolCall,
    ToolResult,
    Turn,
)


# --- Gemini: neutral turn -> Content -------------------------------------


def test_gemini_user_text_becomes_a_single_text_part():
    content = gemini_client._to_content(Turn(role="user", text="hello"))
    assert content.role == "user"
    assert [part.text for part in content.parts] == ["hello"]


def test_gemini_image_turn_carries_bytes_and_mime_type():
    turn = Turn(role="user", text="what is this?",
                images=[ImagePart(data=b"\x89PNG-bytes", mime_type="image/png")])
    content = gemini_client._to_content(turn)

    assert content.parts[0].text == "what is this?"
    blob = content.parts[1].inline_data
    assert blob.mime_type == "image/png"
    assert blob.data == b"\x89PNG-bytes"


def test_gemini_tool_call_and_result_round_trip_into_parts():
    call = Turn(role="model", tool_calls=[ToolCall(name="search_products",
                                                   arguments={"query": "linen shirt"})])
    result = Turn(role="user", tool_results=[ToolResult(name="search_products",
                                                        result={"count": 2})])

    assert gemini_client._to_content(call).parts[0].function_call.name == "search_products"
    response_part = gemini_client._to_content(result).parts[0].function_response
    assert response_part.name == "search_products"
    assert response_part.response == {"count": 2}


# --- Gemini: response -> LLMResponse -------------------------------------


class _Part:
    def __init__(self, text=None, function_call=None, thought=False):
        self.text = text
        self.function_call = function_call
        self.thought = thought


class _Call:
    def __init__(self, name, args):
        self.name = name
        self.args = args
        self.id = None


class _Candidate:
    def __init__(self, parts, finish_reason="STOP"):
        self.content = type("C", (), {"parts": parts})()
        self.finish_reason = finish_reason


class _Response:
    def __init__(self, candidates):
        self.candidates = candidates


def test_gemini_text_response_is_normalised():
    response = gemini_client._to_llm_response(
        _Response([_Candidate([_Part(text="Hello.")])]), "gemini-3.7-flash"
    )
    assert response.text == "Hello."
    assert response.provider == "gemini"
    assert response.model == "gemini-3.7-flash"
    assert response.tool_calls == []


def test_gemini_internal_reasoning_parts_are_not_shown_to_the_customer():
    response = gemini_client._to_llm_response(
        _Response([_Candidate([
            _Part(text="thinking out loud", thought=True),
            _Part(text="The visible answer."),
        ])]),
        "gemini-3.7-flash",
    )
    assert response.text == "The visible answer."


def test_gemini_function_call_is_extracted():
    response = gemini_client._to_llm_response(
        _Response([_Candidate([_Part(function_call=_Call("get_order_status",
                                                          {"order_number": "1042"}))])]),
        "gemini-3.7-flash",
    )
    assert response.text == ""
    assert response.tool_calls[0].name == "get_order_status"
    assert response.tool_calls[0].arguments == {"order_number": "1042"}


def test_gemini_empty_reply_counts_as_unavailable_so_the_chain_moves_on():
    with pytest.raises(LLMUnavailable):
        gemini_client._to_llm_response(_Response([]), "gemini-3.7-flash")
    with pytest.raises(LLMUnavailable):
        gemini_client._to_llm_response(
            _Response([_Candidate([_Part(text="")], finish_reason="MAX_TOKENS")]),
            "gemini-3.7-flash",
        )
