"""Owner dashboard auth: accounts, login, and session handling.

Direct API calls only - no frontend exists yet (Section 7, step 1 of the build order).
"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.modules.admin.auth import repository, service
from app.modules.admin.auth.schemas import OWNER, STAFF

OWNER_USER = "wanas_owner"
OWNER_PASS = "correct horse battery"
STAFF_USER = "wanas_staff"
STAFF_PASS = "another long enough password"


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def owner(client):
    """An owner account, created directly through the service (not the API - there is
    deliberately no "sign up as owner" endpoint)."""
    service.bootstrap_owner()  # no-op unless ADMIN_OWNER_* is set; asserts it's harmless
    account = service._create_account(OWNER_USER, OWNER_PASS, OWNER)
    token = client.post("/admin/api/auth/login",
                        json={"username": OWNER_USER, "password": OWNER_PASS}).json()["token"]
    return account, token


def _auth(token: str) -> dict:
    return {"Authorization": "Bearer " + token}


# --- bootstrap ---------------------------------------------------------------


def test_bootstrap_does_nothing_without_both_env_vars(monkeypatch):
    monkeypatch.setattr(settings, "admin_owner_username", "", raising=False)
    monkeypatch.setattr(settings, "admin_owner_password", "", raising=False)
    service.bootstrap_owner()
    assert repository.list_accounts() == []


def test_bootstrap_creates_the_first_owner(monkeypatch):
    monkeypatch.setattr(settings, "admin_owner_username", "boss", raising=False)
    monkeypatch.setattr(settings, "admin_owner_password", "a very good password", raising=False)
    service.bootstrap_owner()
    accounts = repository.list_accounts()
    assert len(accounts) == 1
    assert accounts[0].username == "boss"
    assert accounts[0].role == OWNER


def test_bootstrap_re_syncs_the_password_on_every_restart(monkeypatch):
    """ADMIN_OWNER_PASSWORD always wins - the owner's call, 2026-08-20."""
    monkeypatch.setattr(settings, "admin_owner_username", "boss", raising=False)
    monkeypatch.setattr(settings, "admin_owner_password", "first password here", raising=False)
    service.bootstrap_owner()

    monkeypatch.setattr(settings, "admin_owner_password", "second password here", raising=False)
    service.bootstrap_owner()  # same username, new password - restarting changes it

    with pytest.raises(service.AuthError):
        service.login("boss", "first password here")
    result = service.login("boss", "second password here")
    assert result.account.username == "boss"
    assert len(repository.list_accounts()) == 1  # still one account, not a duplicate


def test_bootstrap_leaves_a_staff_account_of_the_same_name_alone(monkeypatch):
    service._create_account("boss", "a staff password here", STAFF)
    monkeypatch.setattr(settings, "admin_owner_username", "boss", raising=False)
    monkeypatch.setattr(settings, "admin_owner_password", "an owner password here", raising=False)
    service.bootstrap_owner()

    accounts = repository.list_accounts()
    assert len(accounts) == 1
    assert accounts[0].role == STAFF  # not promoted, not duplicated


def test_bootstrap_ignores_a_too_short_env_password(monkeypatch):
    service._create_account("boss", "the original good password", OWNER)
    monkeypatch.setattr(settings, "admin_owner_username", "boss", raising=False)
    monkeypatch.setattr(settings, "admin_owner_password", "abc", raising=False)
    service.bootstrap_owner()

    result = service.login("boss", "the original good password")
    assert result.account.username == "boss"


# --- login ---------------------------------------------------------------


def test_login_with_the_right_password_succeeds(owner):
    account, token = owner
    assert token


def test_login_with_the_wrong_password_is_refused(owner, client):
    response = client.post("/admin/api/auth/login",
                           json={"username": OWNER_USER, "password": "wrong"})
    assert response.status_code == 401


def test_login_with_an_unknown_username_is_refused_the_same_way(owner, client):
    known = client.post("/admin/api/auth/login",
                        json={"username": OWNER_USER, "password": "wrong"})
    unknown = client.post("/admin/api/auth/login",
                          json={"username": "nobody", "password": "wrong"})
    assert known.status_code == unknown.status_code == 401
    assert known.json() == unknown.json()


def test_a_disabled_account_cannot_log_in():
    service._create_account("gone", "some long password", STAFF)
    account_id = repository.list_accounts(role=STAFF)[0].id
    repository.delete_account(account_id)  # simplest "disabled" this version has: removed

    with pytest.raises(service.AuthError):
        service.login("gone", "some long password")


def test_a_short_password_is_rejected_on_creation():
    with pytest.raises(ValueError):
        service._create_account("shorty", "abc", OWNER)


def test_a_duplicate_username_is_rejected():
    service._create_account("dup", "a decent password here", OWNER)
    with pytest.raises(ValueError):
        service._create_account("dup", "another decent password", STAFF)


# --- sessions --------------------------------------------------------------


def test_a_session_reaches_a_protected_route(owner, client):
    account, token = owner
    response = client.get("/admin/api/auth/me", headers=_auth(token))
    assert response.status_code == 200
    assert response.json()["username"] == OWNER_USER
    assert response.json()["role"] == OWNER


def test_no_token_is_refused(client):
    assert client.get("/admin/api/auth/me").status_code == 401


def test_a_wrong_token_is_refused(client):
    assert client.get("/admin/api/auth/me", headers=_auth("not-a-real-token")).status_code == 401


def test_logout_ends_the_session(owner, client):
    account, token = owner
    assert client.post("/admin/api/auth/logout", headers=_auth(token)).status_code == 200
    assert client.get("/admin/api/auth/me", headers=_auth(token)).status_code == 401


def test_an_expired_session_is_refused(owner):
    import datetime as dt

    account, token = owner
    token_hash = service._hash_token(token)
    repository.delete_session(token_hash)
    repository.create_session(token_hash, account.id,
                              dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1))

    with pytest.raises(service.AuthError):
        service.verify_session(token)


# --- roles: staff management is owner-only ----------------------------------


def test_staff_can_be_created_by_an_owner(owner, client):
    account, token = owner
    response = client.post("/admin/api/auth/staff", headers=_auth(token),
                           json={"username": STAFF_USER, "password": STAFF_PASS})
    assert response.status_code == 201
    assert response.json()["role"] == STAFF


def test_a_staff_account_cannot_create_other_staff(owner, client):
    account, token = owner
    client.post("/admin/api/auth/staff", headers=_auth(token),
               json={"username": STAFF_USER, "password": STAFF_PASS})
    staff_token = client.post("/admin/api/auth/login",
                              json={"username": STAFF_USER, "password": STAFF_PASS}).json()["token"]

    response = client.post("/admin/api/auth/staff", headers=_auth(staff_token),
                           json={"username": "someone_else", "password": "a fine password"})
    assert response.status_code == 403


def test_a_staff_account_cannot_list_or_remove_staff(owner, client):
    account, token = owner
    client.post("/admin/api/auth/staff", headers=_auth(token),
               json={"username": STAFF_USER, "password": STAFF_PASS})
    staff_token = client.post("/admin/api/auth/login",
                              json={"username": STAFF_USER, "password": STAFF_PASS}).json()["token"]

    assert client.get("/admin/api/auth/staff", headers=_auth(staff_token)).status_code == 403
    assert client.delete("/admin/api/auth/staff/1", headers=_auth(staff_token)).status_code == 403


def test_an_owner_lists_and_removes_staff(owner, client):
    account, token = owner
    created = client.post("/admin/api/auth/staff", headers=_auth(token),
                          json={"username": STAFF_USER, "password": STAFF_PASS}).json()

    listed = client.get("/admin/api/auth/staff", headers=_auth(token)).json()["staff"]
    assert [row["username"] for row in listed] == [STAFF_USER]

    removed = client.delete("/admin/api/auth/staff/%d" % created["id"], headers=_auth(token))
    assert removed.status_code == 200
    assert client.get("/admin/api/auth/staff", headers=_auth(token)).json()["staff"] == []


def test_removing_a_staff_account_ends_its_sessions(owner, client):
    account, token = owner
    created = client.post("/admin/api/auth/staff", headers=_auth(token),
                          json={"username": STAFF_USER, "password": STAFF_PASS}).json()
    staff_token = client.post("/admin/api/auth/login",
                              json={"username": STAFF_USER, "password": STAFF_PASS}).json()["token"]

    client.delete("/admin/api/auth/staff/%d" % created["id"], headers=_auth(token))
    assert client.get("/admin/api/auth/me", headers=_auth(staff_token)).status_code == 401


def test_the_remove_staff_endpoint_will_not_touch_an_owner_account(owner, client):
    account, token = owner
    response = client.delete("/admin/api/auth/staff/%d" % account.id, headers=_auth(token))
    assert response.status_code == 404
    assert client.get("/admin/api/auth/me", headers=_auth(token)).status_code == 200


# --- what a password never does ---------------------------------------------


def test_the_password_hash_never_reaches_an_api_response(owner, client):
    account, token = owner
    me = client.get("/admin/api/auth/me", headers=_auth(token)).json()
    login = client.post("/admin/api/auth/login",
                        json={"username": OWNER_USER, "password": OWNER_PASS}).json()
    for payload in (me, login, login["account"]):
        assert "password" not in str(payload).lower().replace("password\":", "")
    assert "password_hash" not in me and "password_hash" not in login["account"]


def test_two_accounts_with_the_same_password_get_different_hashes():
    a = service._create_account("alice", "the same password here", STAFF)
    b = service._create_account("bob", "the same password here", STAFF)
    hash_a = repository.credentials_for("alice").password_hash
    hash_b = repository.credentials_for("bob").password_hash
    assert hash_a != hash_b
