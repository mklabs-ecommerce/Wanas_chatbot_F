"""Types admin.auth passes to callers.

Deliberately excludes anything secret: ``Account`` never carries a password hash, and a
session is handed back once, at login, as the bearer token itself — after that only its
SHA-256 is ever stored or compared.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict

OWNER = "owner"
STAFF = "staff"
ROLES = (OWNER, STAFF)


@dataclass
class Account:
    id: int
    username: str
    role: str
    disabled: bool
    created_at: datetime

    @property
    def is_owner(self) -> bool:
        return self.role == OWNER

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "disabled": self.disabled,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class LoginResult:
    token: str
    expires_at: datetime
    account: Account

    def to_dict(self) -> Dict[str, Any]:
        return {
            "token": self.token,
            "expires_at": self.expires_at.isoformat(),
            "account": self.account.to_dict(),
        }
