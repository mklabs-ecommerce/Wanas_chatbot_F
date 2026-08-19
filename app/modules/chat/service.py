"""The chat module's public surface for the rest of the app.

Everything else in this module is internal: ``agent.py`` runs the turn, ``repository.py``
owns the conversation tables, ``tools.py`` reaches outwards. This file is the one door in.

Today it answers a single question - what was actually said in a conversation - because a
support ticket is read by a person who then has to work out what happened. The ticket's
summary is the *model's* account of the conversation; the transcript is the evidence
behind it, and the two disagreeing is itself worth seeing.
"""

from typing import List, Optional

from app.modules.chat import repository

# Enough to see how an issue arose without turning an email into a novel.
DEFAULT_TRANSCRIPT_LIMIT = 20


def transcript(
    conversation_id: Optional[str],
    limit: int = DEFAULT_TRANSCRIPT_LIMIT,
) -> List[dict]:
    """Return what was said in a conversation, oldest first.

    Rows are ``{"role": "user"|"model", "content": str}``. An unknown conversation is an
    empty list rather than an error: a caller wanting to attach context must not fail
    because there is none.
    """
    if not conversation_id:
        return []
    return repository.get_recent_messages(conversation_id, max(1, limit))


def conversations(limit: int = 50) -> List[dict]:
    """Every recent conversation, newest first, for the owner-facing view.

    Summaries only - the messages themselves come from ``transcript()``.
    """
    return repository.recent_conversations(max(1, limit))
