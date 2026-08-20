"""admin.conversations — read-only conversation listing and detail for the dashboard.

Public surface is ``service.py``. Read-only in this version: no send, no reply, no
takeover - see ``app/modules/admin/__init__.py``. Nothing here should be built in
anticipation of a future reply feature.

Composes other modules' public ``service.py`` the same way ``modules/dashboard`` does -
there is no SQL and no Shopify call in this file. It does not import ``modules/dashboard``
itself: that module's own docstring says nothing may depend on it, so this mirrors its
shape rather than reusing its code.
"""
