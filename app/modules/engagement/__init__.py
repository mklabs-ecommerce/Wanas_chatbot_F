"""Engagement module - Instagram comments and the DM conversations they open.

Owns the decision of what a public comment is and what to do about it, and the mapping
from an Instagram user to a conversation. It does not hold a conversation itself: an
important comment is handed to ``chat.service``, exactly as the web widget's messages
are, so there is one assistant and not two.
"""
