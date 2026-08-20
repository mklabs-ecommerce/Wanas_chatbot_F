"""Owner and staff accounts, and the sessions a logged-in browser carries.

Per the boundary rules this is the only code permitted to query ``admin_accounts`` and
``admin_sessions``. Other modules never see a password hash or a raw session token —
they get ``schemas.Account`` back from ``service.py`` instead.

A session is stored by the SHA-256 of its bearer token, never the token itself, so a
leaked database does not also hand out every live login — the same reason
``modules/dashboard`` compares its token in constant time rather than storing it in the
clear anywhere reachable.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, session_scope
from app.modules.admin.auth.schemas import Account

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AdminAccount(Base):
    __tablename__ = "admin_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16))
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AdminSession(Base):
    __tablename__ = "admin_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("admin_accounts.id"),
                                            index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


@dataclass
class Credentials:
    """Login-only shape — the one place a password hash leaves this file."""
    account: Account
    password_hash: str


def _to_account(row: AdminAccount) -> Account:
    return Account(id=row.id, username=row.username, role=row.role,
                   disabled=row.disabled, created_at=row.created_at)


# -- accounts ---------------------------------------------------------------


def create_account(username: str, password_hash: str, role: str) -> Account:
    """Raises ``ValueError`` if the username is already taken."""
    with session_scope() as session:
        row = AdminAccount(username=username, password_hash=password_hash, role=role)
        session.add(row)
        try:
            session.flush()
        except IntegrityError as exc:
            session.rollback()
            raise ValueError("that username is already taken") from exc
        return _to_account(row)


def credentials_for(username: str) -> Optional[Credentials]:
    """The one lookup allowed to touch a password hash — for verifying a login only."""
    with session_scope() as session:
        row = session.execute(
            select(AdminAccount).where(AdminAccount.username == username)
        ).scalar_one_or_none()
        if row is None:
            return None
        return Credentials(account=_to_account(row), password_hash=row.password_hash)


def get_account(account_id: int) -> Optional[Account]:
    with session_scope() as session:
        row = session.get(AdminAccount, account_id)
        return _to_account(row) if row is not None else None


def list_accounts(role: Optional[str] = None) -> List[Account]:
    with session_scope() as session:
        query = select(AdminAccount).order_by(AdminAccount.created_at)
        if role:
            query = query.where(AdminAccount.role == role)
        return [_to_account(row) for row in session.execute(query).scalars().all()]


def any_owner_exists() -> bool:
    with session_scope() as session:
        return session.execute(
            select(AdminAccount.id).where(AdminAccount.role == "owner")
        ).first() is not None


def delete_account(account_id: int) -> bool:
    with session_scope() as session:
        row = session.get(AdminAccount, account_id)
        if row is None:
            return False
        session.delete(row)
        session.execute(delete(AdminSession).where(AdminSession.account_id == account_id))
        return True


# -- sessions -----------------------------------------------------------------


def create_session(token_hash: str, account_id: int, expires_at: datetime) -> None:
    with session_scope() as session:
        session.add(AdminSession(token_hash=token_hash, account_id=account_id,
                                 expires_at=expires_at))


def account_for_session(token_hash: str) -> Optional[Account]:
    """The account a live, unexpired session belongs to. Expired sessions are dropped."""
    with session_scope() as session:
        row = session.get(AdminSession, token_hash)
        if row is None:
            return None
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            # SQLite drops the offset on the way back out - it was always stored as UTC.
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= _now():
            session.delete(row)
            return None
        account_row = session.get(AdminAccount, row.account_id)
        if account_row is None or account_row.disabled:
            return None
        return _to_account(account_row)


def delete_session(token_hash: str) -> None:
    with session_scope() as session:
        row = session.get(AdminSession, token_hash)
        if row is not None:
            session.delete(row)
