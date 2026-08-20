"""Auth endpoints for the owner dashboard, and the dependencies later admin routers use.

``current_account`` and ``require_owner_account`` are exported so
``modules/admin/analytics`` and ``modules/admin/conversations`` protect their own routes
with the same rule this module enforces on its own — no second auth implementation.
"""

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.modules.admin.auth import service
from app.modules.admin.auth.schemas import Account

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/api/auth", tags=["admin-auth"])


def _bearer_token(authorization: str = Header(default="")) -> str:
    if not authorization.lower().startswith("bearer "):
        return ""
    return authorization[len("bearer "):].strip()


def current_account(authorization: str = Header(default="")) -> Account:
    """FastAPI dependency: the logged-in account, or a 401."""
    token = _bearer_token(authorization)
    try:
        return service.verify_session(token)
    except service.AuthError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Not authenticated")


def require_owner_account(account: Account = Depends(current_account)) -> Account:
    """FastAPI dependency: the logged-in account, and it must be an owner, or a 403."""
    if not account.is_owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Owner only")
    return account


class LoginBody(BaseModel):
    username: str
    password: str


class StaffBody(BaseModel):
    username: str
    password: str = Field(min_length=1)


@router.post("/login")
def login(body: LoginBody) -> dict:
    try:
        result = service.login(body.username, body.password)
    except service.AuthError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid username or password")
    return result.to_dict()


@router.post("/logout")
def logout(authorization: str = Header(default="")) -> dict:
    service.logout(_bearer_token(authorization))
    return {"ok": True}


@router.get("/me")
def me(account: Account = Depends(current_account)) -> dict:
    return account.to_dict()


@router.get("/staff")
def list_staff(_: Account = Depends(require_owner_account)) -> dict:
    return {"staff": [account.to_dict() for account in service.list_staff()]}


@router.post("/staff", status_code=status.HTTP_201_CREATED)
def create_staff(body: StaffBody, owner: Account = Depends(require_owner_account)) -> dict:
    try:
        account = service.create_staff(body.username, body.password, owner)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return account.to_dict()


@router.delete("/staff/{account_id}")
def remove_staff(account_id: int, owner: Account = Depends(require_owner_account)) -> dict:
    try:
        service.remove_staff(account_id, owner)
    except service.AuthError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="No such staff account")
    return {"ok": True}
