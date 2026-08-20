"""admin.auth — owner and staff accounts, login, and session handling.

Public surface is ``service.py``. Nothing outside this package ever sees a password
hash or a raw session token; callers get back ``schemas.Account`` and, at login,
``schemas.LoginResult``.
"""
