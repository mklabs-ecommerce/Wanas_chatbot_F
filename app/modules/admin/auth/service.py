"""Login and session handling for the owner dashboard.

Passwords are hashed with ``hashlib.scrypt`` — stdlib, so this needs no new dependency
the way the rest of the project avoids one wherever the standard library already does
the job. The stored form carries its own parameters (``scrypt$n$r$p$salt$hash``) so a
future change to the cost factor does not invalidate hashes already on disk.

A session token is a random 32-byte URL-safe string handed to the caller exactly once,
at login; only its SHA-256 is ever persisted, which mirrors the reasoning
``modules/dashboard`` uses for comparing its shared token in constant time.

Role checks live here, not in the router: ``require_owner`` is a plain function so a
future internal caller (a script, a test) gets the same rule the API enforces.
"""

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app.core.config import settings
from app.modules.admin.auth import repository
from app.modules.admin.auth.schemas import OWNER, ROLES, STAFF, Account, LoginResult

logger = logging.getLogger(__name__)

MIN_USERNAME_LENGTH = 3
MIN_PASSWORD_LENGTH = 8

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1


class AuthError(Exception):
    """Wrong credentials, an unknown or disabled account, or an invalid session."""


class PermissionDenied(Exception):
    """The caller is authenticated but the action is owner-only."""


# -- passwords ----------------------------------------------------------------


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                             n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return "scrypt$%d$%d$%d$%s$%s" % (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
                                       salt.hex(), derived.hex())


def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                 n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(derived.hex(), hash_hex)
    except (ValueError, TypeError):
        # A malformed stored hash must fail closed, not raise past the caller.
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# -- accounts -------------------------------------------------------------


def _create_account(username: str, password: str, role: str) -> Account:
    username = (username or "").strip()
    if len(username) < MIN_USERNAME_LENGTH:
        raise ValueError("username must be at least %d characters" % MIN_USERNAME_LENGTH)
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise ValueError("password must be at least %d characters" % MIN_PASSWORD_LENGTH)
    if role not in ROLES:
        raise ValueError("unknown role: %r" % role)
    return repository.create_account(username, _hash_password(password), role)


def bootstrap_owner() -> None:
    """Create the first owner account from the environment, if none exists yet.

    Idempotent — safe to call on every startup. Only acts when both
    ``ADMIN_OWNER_USERNAME`` and ``ADMIN_OWNER_PASSWORD`` are set, and only when that
    username is not already taken, so it never overwrites a password someone has since
    changed. It does not require that no owner exists at all — a second, differently
    named owner set in the environment is created too, which is deliberate: it gives a
    way back in if the first owner's password is lost.
    """
    if not (settings.admin_owner_username and settings.admin_owner_password):
        return
    username = settings.admin_owner_username.strip()
    if repository.credentials_for(username) is not None:
        return
    try:
        _create_account(username, settings.admin_owner_password, OWNER)
        logger.info("Created owner account %r from ADMIN_OWNER_USERNAME", username)
    except ValueError as exc:
        logger.warning("Could not bootstrap owner account %r: %s", username, exc)


def create_staff(username: str, password: str, acting_as: Account) -> Account:
    """Owner-only. Raises ``PermissionDenied`` for anyone else."""
    if not acting_as.is_owner:
        raise PermissionDenied("only an owner can create a staff account")
    return _create_account(username, password, STAFF)


def list_staff() -> List[Account]:
    return repository.list_accounts(role=STAFF)


def remove_staff(account_id: int, acting_as: Account) -> None:
    """Owner-only, and only ever removes a *staff* account.

    Refusing to touch an ``owner`` row here — even the caller's own — means this
    endpoint can never be the way the last owner locks themselves out.
    """
    if not acting_as.is_owner:
        raise PermissionDenied("only an owner can remove a staff account")
    account = repository.get_account(account_id)
    if account is None or account.role != STAFF:
        raise AuthError("no such staff account")
    repository.delete_account(account_id)


# -- login / sessions -----------------------------------------------------


def login(username: str, password: str) -> LoginResult:
    """Raises ``AuthError`` for a wrong username, wrong password, or disabled account.

    Deliberately the same error either way — which part was wrong is not something a
    caller trying passwords needs to learn.
    """
    creds = repository.credentials_for((username or "").strip())
    if creds is None or creds.account.disabled:
        # Still runs a hash, so a wrong username does not answer measurably faster than
        # a wrong password would.
        _verify_password(password or "", _DUMMY_HASH)
        raise AuthError("invalid_credentials")
    if not _verify_password(password or "", creds.password_hash):
        raise AuthError("invalid_credentials")

    token = secrets.token_urlsafe(32)
    ttl_hours = max(1.0, settings.admin_session_ttl_hours)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    repository.create_session(_hash_token(token), creds.account.id, expires_at)
    return LoginResult(token=token, expires_at=expires_at, account=creds.account)


def verify_session(token: str) -> Account:
    """Raises ``AuthError`` if the token is missing, unknown, expired or disabled."""
    if not token:
        raise AuthError("no session token")
    account = repository.account_for_session(_hash_token(token))
    if account is None:
        raise AuthError("invalid_session")
    return account


def logout(token: str) -> None:
    if token:
        repository.delete_session(_hash_token(token))


def require_owner(account: Account) -> None:
    if not account.is_owner:
        raise PermissionDenied("owner only")


# A precomputed hash of a password nobody will ever type, spent on every failed login
# so a wrong username costs the same wall-clock time as a wrong password.
_DUMMY_HASH = _hash_password(secrets.token_urlsafe(32))
