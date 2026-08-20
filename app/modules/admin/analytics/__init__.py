"""admin.analytics — the KPI cards and charts behind the owner dashboard.

Public surface is ``service.py``. Everything here reads other modules' data through
their own ``service.py`` functions (orders, chat, support, feedback) - never their
tables directly, per the boundary rules in ``app/modules/__init__.py``.
"""
