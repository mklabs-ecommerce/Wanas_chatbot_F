"""admin module — everything the owner dashboard needs.

Same boundary discipline as the rest of the app (see ``app/modules/__init__.py``):
``admin.analytics`` and ``admin.conversations`` read other modules' data by calling
their public ``service.py`` functions, never by querying another module's tables. If
something analytics needs does not exist yet on the owning module, it is added there —
not worked around here.

``admin.auth`` is the exception in the other direction: it owns ``admin_accounts`` and
``admin_sessions`` outright, and no other module may implement its own login or touch
those tables.

``admin.conversations`` is read-only for every channel except one write path: replying
to and taking over an Instagram conversation. See
``app/modules/admin/conversations/__init__.py`` for why Instagram only, and
``app/modules/chat/agent.py`` for how the bot pauses once the owner has taken over.
"""
