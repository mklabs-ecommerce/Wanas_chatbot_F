"""The ``POST /chat`` endpoint.

Transport only: validate the request, hand it to the agent, shape the response. Any
logic beyond that belongs in ``agent.py`` or in another module's ``service.py``.
"""

import logging

from fastapi import APIRouter, HTTPException, status

from app.core.config import settings
from app.modules.chat import agent, attachments
from app.modules.chat.attachments import AttachmentError
from app.modules.chat.schemas import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Send one customer message and get the assistant's reply."""
    if not settings.gemini_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No LLM provider is configured",
        )

    message = request.message.strip()
    if not message and not request.images:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="send a message, an image, or both",
        )

    try:
        images = attachments.decode_images(request.images)
    except AttachmentError as exc:
        # These messages are written to be shown to a customer, so the widget can
        # display the reason instead of a generic failure.
        logger.info("Rejected an upload: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    reply = await agent.handle_message(
        message=message,
        images=images,
        conversation_id=request.conversation_id,
        channel=request.channel,
    )
    return ChatResponse(
        conversation_id=reply.conversation_id,
        reply=reply.text,
        model=reply.model,
        provider=reply.provider,
        degraded=reply.degraded,
        tools_used=reply.tools_used,
    )
