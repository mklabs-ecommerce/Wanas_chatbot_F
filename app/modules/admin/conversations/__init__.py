"""admin.conversations — conversation listing, detail, and Instagram reply/takeover.

Public surface is ``service.py``. Listing and detail stay read-only for every channel.
One write path exists: the owner can reply to an Instagram conversation from the
dashboard, which pauses the bot for that conversation until it is handed back
(``chat.service.post_owner_message`` / ``resume_bot``, reached through
``engagement.service.send_owner_reply`` so the actual send still goes through the one
module that owns talking to Instagram). It is Instagram-only because that channel is the
only one with a push path to the customer outside the request that opened this app's own
door - the web widget only ever gets a reply inside its own POST /chat response, so a
web reply from here would need polling added to that customer-facing file, which is a
separate, not-yet-taken decision. Do not build a web write path without addressing that.

Composes other modules' public ``service.py`` the same way the older, single-shared-token
``modules/dashboard`` did before it was removed (superseded by this module plus the React
frontend, 2026-08-20) - there is no SQL and no Shopify call in this file.
"""
