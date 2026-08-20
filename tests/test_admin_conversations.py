"""admin.conversations: read-only listing and detail for the owner dashboard (step 5).

Mirrors tests/test_dashboard.py's shape - same underlying composition - but exercises
the account-based auth, the per-channel routes, and the "all" aggregated view instead
of the older shared-token dashboard.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.admin.auth import service as auth_service
from app.modules.admin.auth.schemas import OWNER
from app.modules.chat import repository as chat_repository
from app.modules.orders import repository as orders_repository


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def logged_in(client):
    auth_service._create_account("owner1", "a fine password here", OWNER)
    result = auth_service.login("owner1", "a fine password here")
    return {"Authorization": "Bearer " + result.token}


def _web(cid_suffix=""):
    return chat_repository.ensure_conversation("web-" + cid_suffix if cid_suffix else None,
                                               channel="web")


def _instagram(cid_suffix=""):
    return chat_repository.ensure_conversation(
        "ig-" + cid_suffix if cid_suffix else None, channel="instagram")


# --- who gets in -------------------------------------------------------------


def test_every_route_requires_a_session(client):
    assert client.get("/admin/api/conversations/web").status_code == 401
    assert client.get("/admin/api/conversations/web/anything").status_code == 401


def test_an_unknown_channel_is_refused(logged_in, client):
    assert client.get("/admin/api/conversations/snapchat", headers=logged_in).status_code == 400
    assert client.get("/admin/api/conversations/snapchat/x",
                      headers=logged_in).status_code == 400


# --- listing, per channel ----------------------------------------------------


def test_an_empty_install_shows_no_conversations(logged_in, client):
    body = client.get("/admin/api/conversations/web", headers=logged_in).json()
    assert body == {"channel": "web", "count": 0, "sort": "recent", "conversations": []}


def test_a_conversation_appears_with_its_message_count(logged_in, client):
    cid = _web()
    chat_repository.add_message(cid, chat_repository.ROLE_USER, "عندكم هودي؟")
    chat_repository.add_message(cid, chat_repository.ROLE_MODEL, "أيوه عندنا")

    body = client.get("/admin/api/conversations/web", headers=logged_in).json()
    assert body["count"] == 1
    row = body["conversations"][0]
    assert row["conversation_id"] == cid
    assert row["channel"] == "web"
    assert row["message_count"] == 2
    assert row["last_message"] == "أيوه عندنا"


def test_a_channel_only_shows_its_own_conversations(logged_in, client):
    web_cid = _web()
    ig_cid = _instagram()
    chat_repository.add_message(web_cid, chat_repository.ROLE_USER, "hi")
    chat_repository.add_message(ig_cid, chat_repository.ROLE_USER, "hi")

    web_ids = [c["conversation_id"]
              for c in client.get("/admin/api/conversations/web",
                                  headers=logged_in).json()["conversations"]]
    ig_ids = [c["conversation_id"]
             for c in client.get("/admin/api/conversations/instagram",
                                 headers=logged_in).json()["conversations"]]
    assert web_ids == [web_cid]
    assert ig_ids == [ig_cid]


def test_all_aggregates_every_channel(logged_in, client):
    web_cid = _web()
    ig_cid = _instagram()
    chat_repository.add_message(web_cid, chat_repository.ROLE_USER, "hi")
    chat_repository.add_message(ig_cid, chat_repository.ROLE_USER, "hi")

    body = client.get("/admin/api/conversations/all", headers=logged_in).json()
    assert body["channel"] == "all"
    assert sorted(c["conversation_id"] for c in body["conversations"]) == sorted([web_cid, ig_cid])


@pytest.mark.parametrize("channel", ["whatsapp", "tiktok", "facebook"])
def test_a_placeholder_channel_shows_an_empty_list_not_an_error(logged_in, client, channel):
    _web()  # something exists, just not on this channel
    body = client.get("/admin/api/conversations/%s" % channel, headers=logged_in).json()
    assert body == {"channel": channel, "count": 0, "sort": "recent", "conversations": []}


def test_orders_placed_in_a_conversation_are_counted(logged_in, client):
    cid = _web()
    chat_repository.add_message(cid, chat_repository.ROLE_USER, "hi")
    orders_repository.link(cid, "#1008", "web")

    row = client.get("/admin/api/conversations/web",
                     headers=logged_in).json()["conversations"][0]
    assert row["order_count"] == 1


def test_the_list_view_never_calls_shopify(logged_in, client, monkeypatch):
    """It must stay fast however many conversations there are."""
    from app.modules.admin.conversations import service as admin_conversations_service

    async def explode(*_a, **_k):
        raise AssertionError("the list view called Shopify")

    monkeypatch.setattr(admin_conversations_service.orders_service, "lookup_for_staff", explode)
    monkeypatch.setattr(admin_conversations_service.orders_service, "orders_for_conversation",
                        explode)
    cid = _web()
    chat_repository.add_message(cid, chat_repository.ROLE_USER, "hi")
    orders_repository.link(cid, "#1008", "web")

    assert client.get("/admin/api/conversations/web", headers=logged_in).status_code == 200


# --- detail -------------------------------------------------------------


def test_an_unknown_conversation_is_404(logged_in, client):
    response = client.get("/admin/api/conversations/web/does-not-exist", headers=logged_in)
    assert response.status_code == 404


def test_a_conversation_with_no_messages_yet_is_not_404(logged_in, client):
    """Told apart from an unknown id - this is a real, empty conversation."""
    cid = _web()
    response = client.get("/admin/api/conversations/web/" + cid, headers=logged_in)
    assert response.status_code == 200
    assert response.json()["messages"] == []


def test_asking_for_a_conversation_under_the_wrong_channel_tab_is_404(logged_in, client):
    cid = _web()
    chat_repository.add_message(cid, chat_repository.ROLE_USER, "hi")
    assert client.get("/admin/api/conversations/instagram/" + cid,
                      headers=logged_in).status_code == 404
    assert client.get("/admin/api/conversations/web/" + cid,
                      headers=logged_in).status_code == 200


def test_all_reaches_a_conversation_from_any_channel(logged_in, client):
    cid = _instagram()
    chat_repository.add_message(cid, chat_repository.ROLE_USER, "hi")
    assert client.get("/admin/api/conversations/all/" + cid,
                      headers=logged_in).status_code == 200


async def test_shopify_being_down_costs_the_orders_not_the_page(monkeypatch):
    from app.modules.admin.conversations import service as admin_conversations_service

    async def explode(_conversation_id):
        raise RuntimeError("shopify is unreachable")

    monkeypatch.setattr(admin_conversations_service.orders_service, "orders_for_conversation",
                        explode)
    cid = _web()
    chat_repository.add_message(cid, chat_repository.ROLE_USER, "hi")

    detail = await admin_conversations_service.get_conversation(cid)
    assert detail["orders"] == []
    assert detail["orders_readable"] is False
    assert len(detail["messages"]) == 1


async def test_no_orders_and_unreadable_orders_are_different_facts():
    from app.modules.admin.conversations import service as admin_conversations_service

    cid = _web()
    detail = await admin_conversations_service.get_conversation(cid)
    assert detail["orders"] == []
    assert detail["orders_readable"] is True


# --- read-only: no reply, no takeover ----------------------------------------


def test_there_is_no_way_to_send_a_message_through_this_module(logged_in, client):
    cid = _web()
    for method in (client.post, client.put, client.patch):
        response = method("/admin/api/conversations/web/" + cid, headers=logged_in,
                          json={"text": "hello"})
        assert response.status_code in (404, 405)
    assert client.delete("/admin/api/conversations/web/" + cid,
                         headers=logged_in).status_code in (404, 405)


# --- staff can read too (Section 5: only settings/accounts are owner-only) --


def test_a_staff_account_can_list_and_read_conversations(logged_in, client):
    client.post("/admin/api/auth/staff", headers=logged_in,
               json={"username": "staffer", "password": "a fine password too"})
    staff_token = client.post("/admin/api/auth/login",
                              json={"username": "staffer",
                                    "password": "a fine password too"}).json()["token"]
    staff_headers = {"Authorization": "Bearer " + staff_token}

    cid = _web()
    chat_repository.add_message(cid, chat_repository.ROLE_USER, "hi")

    assert client.get("/admin/api/conversations/web", headers=staff_headers).status_code == 200
    assert client.get("/admin/api/conversations/web/" + cid,
                      headers=staff_headers).status_code == 200
