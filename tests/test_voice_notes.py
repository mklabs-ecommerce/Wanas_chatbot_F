"""Voice notes.

A spoken message is transcribed first and then treated exactly like a typed one. Most of
what is asserted here follows from that single decision: the transcript is what reaches
the history, the tools and the assistant, and nothing downstream learns that a microphone
was involved.

The rest is about not trusting the transcript. Speech gets misheard, and the thing an
Egyptian customer is most likely to say out loud is a phone number.
"""

import base64
import math
import struct

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.integrations.llm_types import AudioPart, LLMResponse, LLMUnavailable
from app.main import app
from app.modules.chat import agent, attachments, voice
from app.modules.chat import repository as chat_repository
from app.modules.chat.attachments import AttachmentError
from app.modules.chat.schemas import VoiceUpload


def _wav(seconds: float = 1.0, rate: int = 16000, amplitude: int = 8000) -> bytes:
    """A real WAV file - header and all, so sniffing sees the truth.

    It carries an actual waveform by default, because a silent one is refused, which is
    the whole point of ``carries_sound``. Pass ``amplitude=0`` for silence.
    """
    frames = int(rate * seconds)
    samples = [int(amplitude * math.sin(i / 8)) for i in range(frames)]
    data = struct.pack("<" + str(frames) + "h", *samples)
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(data)) + data)


def _silence(seconds: float = 1.0) -> bytes:
    return _wav(seconds=seconds, amplitude=0)


def _upload(data: bytes = None, mime: str = None, as_data_url: bool = False):
    """The exact shape the widget posts: base64 in the JSON body."""
    encoded = base64.b64encode(data if data is not None else _wav()).decode()
    if as_data_url:
        encoded = "data:" + (mime or "audio/wav") + ";base64," + encoded
    return VoiceUpload(data=encoded, mime_type=mime)


def _body(upload) -> dict:
    """The same upload as the JSON a browser would send."""
    return upload.model_dump(exclude_none=True)


@pytest.fixture
def heard(monkeypatch):
    """Make transcription return a fixed sentence without touching the network."""
    said = {"text": "عايز التيشيرت البني مقاس M"}

    async def transcribe(clips):
        assert clips, "transcribe was called with no audio"
        return voice.Transcript(text=said["text"], model="test-ears")

    monkeypatch.setattr(voice, "transcribe", transcribe)
    return said


@pytest.fixture
def replies(monkeypatch):
    """Capture what the chat model is asked, and answer briefly."""
    seen = {}

    async def generate(**kwargs):
        seen.update(kwargs)
        return LLMResponse(text="تمام يا فندم", model="test", provider="gemini")

    monkeypatch.setattr(agent.llm, "generate", generate)
    return seen


# --- decoding what arrives -------------------------------------------------


def test_a_real_wav_is_accepted():
    parts = attachments.decode_audio([_upload()])
    assert len(parts) == 1
    assert parts[0].mime_type == "audio/wav"


def test_the_declared_type_is_never_trusted():
    """Gemini works the type out itself, so this file is the only thing checking."""
    parts = attachments.decode_audio([_upload(mime="audio/ogg")])
    assert parts[0].mime_type == "audio/wav"


def test_a_codec_parameter_on_the_declared_type_is_tolerated():
    """MediaRecorder reports 'audio/webm;codecs=opus'."""
    parts = attachments.decode_audio([_upload(mime="audio/webm;codecs=opus")])
    assert parts[0].mime_type == "audio/wav"


def test_a_data_url_is_accepted():
    """FileReader hands the widget "data:audio/wav;base64,..." - the exact shape sent."""
    parts = attachments.decode_audio([_upload(as_data_url=True)])
    assert parts[0].mime_type == "audio/wav"


def test_something_that_is_not_audio_is_refused():
    with pytest.raises(AttachmentError):
        attachments.decode_audio([_upload(data=b"\xff\xd8\xff" + b"\x00" * 4000)])


def test_a_recording_too_short_to_hold_speech_is_refused():
    with pytest.raises(AttachmentError):
        attachments.decode_audio([_upload(data=b"RIFF\x04\x00\x00\x00WAVE")])


def test_an_oversized_recording_is_refused_on_its_encoded_length():
    """Rejected before it is expanded in memory."""
    huge = "A" * (attachments.MAX_AUDIO_BYTES * 4 // 3 + 100)
    with pytest.raises(AttachmentError):
        attachments.decode_audio([VoiceUpload(data=huge)])


def test_one_voice_note_per_turn():
    with pytest.raises(AttachmentError):
        attachments.decode_audio([_upload(), _upload()])


def test_no_audio_is_not_an_error():
    assert attachments.decode_audio([]) == []
    assert attachments.decode_audio(None) == []


def test_the_formats_a_whatsapp_adapter_will_hand_over_are_recognised():
    """WhatsApp voice notes are ogg/opus; nothing else in the app cares yet."""
    assert attachments.sniff_audio_type(b"OggS" + b"\x00" * 20) == "audio/ogg"
    assert attachments.sniff_audio_type(b"\x00\x00\x00 ftypM4A " + b"\x00" * 12) == "audio/mp4"
    assert attachments.sniff_audio_type(b"ID3" + b"\x00" * 20) == "audio/mpeg"
    assert attachments.sniff_audio_type(b"fLaC" + b"\x00" * 20) == "audio/flac"


# --- reading the transcription back ----------------------------------------


def test_a_plain_json_transcript_is_read():
    assert voice._parse('{"heard_speech": true, "transcript": "عايز هودي"}') == "عايز هودي"


def test_a_transcript_wrapped_in_prose_is_still_read():
    raw = 'Here you go:\n```json\n{"heard_speech": true, "transcript": "hello"}\n```'
    assert voice._parse(raw) == "hello"


def test_no_speech_reads_as_empty_not_as_words():
    assert voice._parse('{"heard_speech": false, "transcript": ""}') == ""


def test_unparseable_output_is_told_apart_from_silence():
    """None means "the model failed"; "" means "there was nothing to hear"."""
    assert voice._parse("sorry, I cannot do that") is None
    assert voice._parse("") is None


async def test_silence_is_reported_rather_than_answered(monkeypatch):
    async def generate(**_kwargs):
        return LLMResponse(text='{"heard_speech": false, "transcript": ""}',
                           model="m", provider="gemini")

    monkeypatch.setattr(voice.llm, "generate", generate)
    with pytest.raises(voice.VoiceUnavailable):
        await voice.transcribe([AudioPart(data=_wav(), mime_type="audio/wav")])


async def test_a_transcription_failure_raises_rather_than_answering_nothing(monkeypatch):
    """A turn that was entirely spoken has nothing left to reply to."""
    async def generate(**_kwargs):
        raise LLMUnavailable("out of quota")

    monkeypatch.setattr(voice.llm, "generate", generate)
    with pytest.raises(voice.VoiceUnavailable):
        await voice.transcribe([AudioPart(data=_wav(), mime_type="audio/wav")])


async def test_transcription_runs_at_zero_temperature(monkeypatch):
    """A warmer setting invents plausible words over unclear audio."""
    seen = {}

    async def generate(**kwargs):
        seen.update(kwargs)
        return LLMResponse(text='{"heard_speech": true, "transcript": "hi"}',
                           model="m", provider="gemini")

    monkeypatch.setattr(voice.llm, "generate", generate)
    await voice.transcribe([AudioPart(data=_wav(), mime_type="audio/wav")])

    assert seen["temperature"] == 0.0
    assert seen["json_output"] is True
    # No tools: transcription is listening, not deciding.
    assert not seen.get("tools")


async def test_transcription_can_run_on_its_own_model(monkeypatch):
    """So a voice turn does not spend the conversation model's daily allowance."""
    seen = {}

    async def generate(**kwargs):
        seen.update(kwargs)
        return LLMResponse(text='{"heard_speech": true, "transcript": "hi"}',
                           model="m", provider="gemini")

    monkeypatch.setattr(voice.llm, "generate", generate)
    monkeypatch.setattr(settings, "gemini_transcription_model", "gemini-3.7-flash",
                        raising=False)
    await voice.transcribe([AudioPart(data=_wav(), mime_type="audio/wav")])

    assert seen["model"] == "gemini-3.7-flash"


# --- the turn itself -------------------------------------------------------


async def test_the_assistant_answers_the_words_not_the_recording(heard, replies):
    reply = await agent.handle_message("", audio=[AudioPart(data=_wav(),
                                                            mime_type="audio/wav")])

    assert reply.transcript == "عايز التيشيرت البني مقاس M"
    # The chat model was given the transcript as ordinary text.
    turns = replies["turns"]
    assert "عايز التيشيرت البني مقاس M" in turns[-1].text
    assert not turns[-1].audio


async def test_the_recording_never_reaches_the_conversation_model(heard, replies):
    """It has already been listened to; sending it again would pay for it twice."""
    await agent.handle_message("", audio=[AudioPart(data=_wav(), mime_type="audio/wav")])
    assert all(not turn.audio for turn in replies["turns"])


async def test_the_transcript_is_what_goes_into_the_history(heard, replies):
    """Unlike a photo, which is stored as a placeholder - the words are the message."""
    reply = await agent.handle_message("", audio=[AudioPart(data=_wav(),
                                                            mime_type="audio/wav")])
    stored = chat_repository.get_recent_messages(reply.conversation_id, 10)

    assert "عايز التيشيرت البني مقاس M" in stored[0]["content"]
    assert stored[0]["content"].startswith(voice.VOICE_PREFIX)


async def test_a_later_turn_can_still_see_what_was_said(heard, replies):
    """The point of storing the words: "the one I told you about" has to mean something."""
    first = await agent.handle_message("", audio=[AudioPart(data=_wav(),
                                                            mime_type="audio/wav")])
    await agent.handle_message("أيوه ده", conversation_id=first.conversation_id)

    replayed = " ".join(turn.text for turn in replies["turns"])
    assert "عايز التيشيرت البني مقاس M" in replayed


async def test_typing_and_speaking_in_one_turn_keeps_both(heard, replies):
    reply = await agent.handle_message("شوف ده", audio=[AudioPart(data=_wav(),
                                                                  mime_type="audio/wav")])
    stored = chat_repository.get_recent_messages(reply.conversation_id, 10)

    assert "شوف ده" in stored[0]["content"]
    assert "عايز التيشيرت البني مقاس M" in stored[0]["content"]


async def test_a_failed_transcription_stores_nothing(monkeypatch, replies):
    """A retry should read as a first attempt, not a second."""
    async def transcribe(_clips):
        raise voice.VoiceUnavailable("could not hear that")

    monkeypatch.setattr(voice, "transcribe", transcribe)
    reply = await agent.handle_message("", audio=[AudioPart(data=_wav(),
                                                            mime_type="audio/wav")])

    assert reply.text == "could not hear that"
    assert chat_repository.get_recent_messages(reply.conversation_id, 10) == []
    # And the conversation model was never asked, so no quota was spent on it.
    assert replies == {}


async def test_a_typed_turn_carries_no_transcript(replies):
    reply = await agent.handle_message("عندكم هودي؟")
    assert reply.transcript is None


# --- through the endpoint --------------------------------------------------


def test_a_voice_note_can_be_the_whole_message(heard, replies):
    body = TestClient(app).post("/chat", json={"audio": [_body(_upload())]}).json()

    assert body["transcript"] == "عايز التيشيرت البني مقاس M"
    assert body["reply"] == "تمام يا فندم"


def test_a_turn_with_nothing_at_all_is_refused():
    response = TestClient(app).post("/chat", json={})
    assert response.status_code == 422


def test_a_rejected_recording_explains_itself_to_the_customer():
    response = TestClient(app).post("/chat",
                                    json={"audio": [_body(_upload(data=b"\x00" * 4000))]})
    assert response.status_code == 422
    # The wording is written to be shown in the chat window, not logged.
    assert "type your message" in response.json()["detail"]


def test_audio_is_refused_when_voice_notes_are_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "voice_notes", False, raising=False)
    response = TestClient(app).post("/chat", json={"audio": [_body(_upload())]})

    assert response.status_code == 422
    assert "not switched on" in response.json()["detail"]


def test_typed_messages_still_work_with_voice_notes_off(monkeypatch, replies):
    monkeypatch.setattr(settings, "voice_notes", False, raising=False)
    response = TestClient(app).post("/chat", json={"message": "عندكم هودي؟"})
    assert response.status_code == 200


# --- what the assistant is told about speech -------------------------------


def test_the_prompt_says_a_transcript_can_be_wrong():
    prompt = agent.build_system_prompt()

    assert "WHEN A CUSTOMER SENDS A VOICE NOTE" in prompt
    assert "read back" in prompt
    assert "digit by digit" in prompt


def test_the_prompt_forbids_talking_about_the_transcription():
    prompt = agent.build_system_prompt()
    assert "Never mention the recording" in prompt


def test_the_language_rule_is_still_restated_near_the_end():
    """Prompt position is load-bearing; a new section must not push this out."""
    prompt = agent.build_system_prompt()
    assert prompt.rfind("everyday Egyptian") / len(prompt) > 0.8


# --- silence is caught here, not by the model ------------------------------
#
# Measured 2026-08-19: asked to transcribe one second of digital silence,
# gemini-3.7-flash invented a plausible Egyptian sentence three times out of three, and
# the assistant answered a message the customer never sent. The prompt forbidding that
# did not hold, so the check is in code.


def test_digital_silence_carries_no_sound():
    assert attachments.carries_sound(_silence()) is False


def test_a_room_too_quiet_to_have_speech_in_it_carries_no_sound():
    assert attachments.carries_sound(_wav(amplitude=100)) is False


def test_a_softly_spoken_note_is_not_thrown_away():
    """The gate must be conservative - losing a quiet customer is the worse failure."""
    assert attachments.carries_sound(_wav(amplitude=2000)) is True


def test_one_click_in_an_otherwise_silent_clip_is_still_silence():
    frames = 16000
    samples = [0] * frames
    for i in range(5):
        samples[i] = 20000
    data = struct.pack("<" + str(frames) + "h", *samples)
    clip = (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
            + b"data" + struct.pack("<I", len(data)) + data)
    assert attachments.carries_sound(clip) is False


def test_a_recording_we_cannot_measure_is_given_the_benefit_of_the_doubt():
    """Compressed audio cannot be read without decoding it; guessing would be worse."""
    assert attachments.carries_sound(b"OggS" + b"\x00" * 4000) is True


def test_a_silent_recording_is_refused_before_any_model_sees_it():
    with pytest.raises(AttachmentError) as refused:
        attachments.decode_audio([_upload(data=_silence())])
    assert "silent" in str(refused.value)


def test_the_silence_refusal_reaches_the_customer(monkeypatch):
    body = {"audio": [_body(_upload(data=_silence()))]}
    response = TestClient(app).post("/chat", json=body)

    assert response.status_code == 422
    assert "silent" in response.json()["detail"]
