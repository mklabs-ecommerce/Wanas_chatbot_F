"""Turning a customer's voice note into the words they said.

A spoken message is transcribed **before** the conversation runs, and the transcript is
what the rest of the turn sees: the assistant answers text, the history stores text, a
support ticket quotes text. That costs one extra model request per voice note, and buys
three things that matter more:

- the conversation keeps working. A photo can be forgotten between turns because a photo
  is rarely the point of the message; a voice note *is* the message, and "the one I told
  you about" has to still mean something next turn.
- everything downstream is unchanged. Tools, tickets and feedback all receive real words
  and never learn that a microphone was involved.
- the owner can read the conversation. A dashboard transcript full of "[voice note]"
  tells them nothing about why a customer was unhappy.

The transcription runs on its own model (``GEMINI_TRANSCRIPTION_MODEL``) for the same
reason image matching does: it is a different skill from holding a conversation, and on
the free tier each model carries its own request budget, so putting it elsewhere does not
eat into the allowance for replying.

**A transcript is not evidence.** Speech recognition mishears, and the one thing an
Egyptian customer is most likely to say aloud is a phone number. Nothing here decides
that a transcript is right - the prompt requires the assistant to read anything spoken
back before it goes anywhere near an order.
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional, Sequence

from app.core.config import settings
from app.integrations import llm
from app.integrations.llm_types import AudioPart, LLMError, Turn

logger = logging.getLogger(__name__)

# Marks a transcript in the stored history, so the owner reading a conversation back can
# see which messages were spoken - mishearings read very differently once you know.
VOICE_PREFIX = "[voice]"

_SYSTEM = """You transcribe voice notes sent to an Egyptian clothing shop's chat.

Write down exactly what the speaker said, and nothing else.

- Most speakers use everyday Egyptian Arabic. Write it as they said it, in Arabic script. Do not translate it, do not turn it into formal Arabic, and do not tidy up their grammar.
- If they speak English, write English. If they mix the two, write the mix.
- Write numbers as digits. A phone number said as "zero one zero two one" is 01021.
- Write only the words. No summary, no explanation, no answer to what they asked, no notes about the audio, no quotation marks around it.
- If there is no speech at all - silence, noise, music - say so with heard_speech false rather than inventing words.
- Never guess at words you cannot make out. Leave them out.

Reply with JSON only:
{"heard_speech": true or false, "transcript": "the words, or an empty string"}"""

_PROMPT = "Transcribe this voice note."

# Models sometimes wrap JSON in prose or a code fence whatever the instruction says.
_JSON = re.compile(r"\{.*\}", re.DOTALL)


class VoiceUnavailable(Exception):
    """The recording could not be transcribed. The message is safe to show a customer."""


@dataclass
class Transcript:
    """What was heard in a voice note."""

    text: str
    model: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


async def transcribe(clips: Sequence[AudioPart]) -> Transcript:
    """Transcribe one voice note, or raise ``VoiceUnavailable``.

    Raises rather than returning empty on failure, deliberately: a turn whose entire
    content was spoken has nothing left to answer if the words are lost, and silently
    continuing would have the assistant reply to a message it never received.
    """
    clips = list(clips or [])
    if not clips:
        return Transcript(text="")

    try:
        response = await llm.generate(
            turns=[Turn(role="user", text=_PROMPT, audio=clips)],
            system_instruction=_SYSTEM,
            json_output=True,
            # Transcription is a listening task; there is nothing here to be creative
            # about, and a warmer setting invents plausible words over unclear audio.
            temperature=0.0,
            model=settings.gemini_transcription_model or None,
        )
    except LLMError as exc:
        logger.warning("Could not transcribe a voice note: %s", exc)
        raise VoiceUnavailable(
            "معلش، مش قادر أسمع الرسالة الصوتية دلوقتي. ممكن تكتبلي اللي عايزه؟\n"
            "Sorry - I cannot listen to voice notes right now. Could you type it instead?"
        ) from exc

    heard = _parse(response.text)
    if heard is None:
        logger.warning("Transcription returned unusable output: %r", response.text[:300])
        raise VoiceUnavailable(_UNCLEAR)
    if not heard.strip():
        # Silence, background noise, or a record button pressed by accident. Saying so is
        # the honest answer; answering anyway would mean answering nothing.
        logger.info("A voice note contained no speech")
        raise VoiceUnavailable(_UNCLEAR)

    logger.info("Transcribed a voice note (%d characters) via %s",
                len(heard), response.model)
    return Transcript(text=heard.strip(), model=response.model)


_UNCLEAR = (
    "معلش، مش قادر أسمع حاجة في الرسالة الصوتية. ممكن تبعتها تاني أو تكتبلي؟\n"
    "Sorry - I could not make out anything in that recording. Could you send it again, "
    "or type it?"
)


def _parse(raw: str) -> Optional[str]:
    """The transcript out of the model's JSON, or None if it returned nothing usable."""
    candidate = (raw or "").strip()
    if not candidate:
        return None

    parsed = _loads(candidate)
    if parsed is None:
        found = _JSON.search(candidate)
        parsed = _loads(found.group(0)) if found else None
    if not isinstance(parsed, dict):
        return None

    if parsed.get("heard_speech") is False:
        return ""
    text = parsed.get("transcript")
    return text if isinstance(text, str) else None


def _loads(candidate: str):
    try:
        return json.loads(candidate)
    except (ValueError, TypeError):
        return None


def stored_text(transcript: str) -> str:
    """How a spoken message is written into the conversation history."""
    return (VOICE_PREFIX + " " + transcript.strip()).strip()
