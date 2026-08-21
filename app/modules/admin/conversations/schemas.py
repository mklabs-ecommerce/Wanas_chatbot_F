"""Request shapes for the one write path this module has - see service.reply()."""

from pydantic import BaseModel, Field


class ReplyBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)
