from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from bruno.api import create_app
from bruno.models import Conversation, Customer, Order, Outreach
from tests.conftest import DAVE_PHONE, make_customer, make_settings


@pytest.fixture
def client(deps, session_factory):
    return TestClient(create_app(deps, session_factory))


def sendblue_inbound(content, handle="m1", number=DAVE_PHONE):
    return {
        "accountEmail": "demo@example.com", "content": content, "is_outbound": False, "status": "RECEIVED",
        "message_handle": handle, "from_number": number, "number": number, "to_number": "+15550000000",
        "service": "iMessage",
    }


def test_health(client):
    assert client.get("/health").json()["llm"] == "mock"


def test_inbound_text_webhook_runs_agent(client, deps, session, settings):
    make_customer(session, settings)
    r = client.post("/webhook/incoming", json=sendblue_inbound("can I get my usual"))
    assert r.json() == {"status": "accepted"}
    to, reply = deps.messenger.sent[-1]
    assert to == DAVE_PHONE and "$22.14" in reply

    client.post("/webhook/incoming", json=sendblue_inbound("yes", handle="m2"))
    assert "Dave" in deps.messenger.sent[-1][1]
    assert len(deps.pos.orders) == 1


def test_outbound_status_payloads_are_ignored(client, deps):
    r = client.post("/webhook/incoming", json={**sendblue_inbound("hi"), "is_outbound": True})
    assert r.json() == {"status": "ignored"}
    assert deps.messenger.sent == []


def test_webhook_secret(deps, session_factory):
    d = replace(deps, settings=make_settings(webhook_secret="s3cret"))
    c = TestClient(create_app(d, session_factory))
    assert c.post("/webhook/incoming", json=sendblue_inbound("hi")).status_code == 401
    assert c.post("/webhook/incoming?secret=s3cret", json=sendblue_inbound("hi")).status_code == 200


def test_retell_inbound_returns_context(client, session, settings):
    make_customer(session, settings)
    r = client.post(
        "/voice/retell/inbound",
        json={"event": "call_inbound", "call_inbound": {"from_number": DAVE_PHONE, "to_number": "+15550000000"}},
    )
    dv = r.json()["call_inbound"]["dynamic_variables"]
    assert dv["greeting"] == "Hey Dave! Want me to spin up that usual large pepperoni pizza?"
    assert dv["payment_method"] == "Visa •4242"
    assert all(isinstance(v, str) for v in dv.values())


def test_retell_functions_place_order(client, deps, session, settings):
    make_customer(session, settings)
    call = {"from_number": DAVE_PHONE, "call_id": "c1"}
    body = client.post(
        "/voice/retell/functions/update_cart",
        json={"name": "update_cart", "call": call,
              "args": {"items": [{"item_id": "pizza", "size": "large", "quantity": 1, "modifiers": ["pepperoni"]}]}},
    ).json()
    assert body["ok"] and "Large Pizza w/ Pepperoni" in body["summary"]
    r = client.post("/voice/retell/functions/place_order",
                    json={"name": "place_order", "call": call, "args": {"customer_confirmed": True}})
    assert r.json()["ok"]
    assert deps.pos.orders[0].channel == "voice"


def test_pos_feed_updates_learning(client, session, settings):
    make_customer(session, settings)  # drifting
    r = client.post("/webhook/pos/order", json={"phone": DAVE_PHONE, "external_id": "sq-123",
                                                "item_text": "Lg Pepperoni Pizza"})
    assert r.json()["drifting"] is False and r.json()["segment"] == "regular"
    assert client.post("/webhook/pos/order", json={"phone": DAVE_PHONE, "external_id": "sq-123"}).json() == {"status": "duplicate"}
    session.expire_all()
    assert session.query(Order).filter_by(external_id="sq-123").one().items_json[0]["modifiers"] == ["pepperoni"]


# --------------------------------------------------------------------------- dashboard
def login(client, password="pizza-pass-1"):
    return client.post("/admin/login", data={"password": password}, follow_redirects=False)


def test_dashboard_requires_login_and_first_run_password(client):
    assert client.get("/admin", follow_redirects=False).headers["location"] == "/admin/login"
    assert "Create" in client.get("/admin/login").text
    assert login(client, "short").headers["location"] == "/admin/login"
    assert login(client).headers["location"] == "/admin"
    assert client.get("/admin").status_code == 200
    client.get("/admin/logout")
    assert login(client, "wrong-password").headers["location"] == "/admin/login"
    assert login(client).headers["location"] == "/admin"


def test_dashboard_pages_render(client, session, settings, deps):
    make_customer(session, settings)
    client.post("/webhook/incoming", json=sendblue_inbound("can I get my usual"))
    login(client)
    conv_id = session.query(Conversation).one().id
    cust_id = session.query(Customer).one().id
    for path in ["/admin", "/admin/settings", "/admin/import", "/admin/menu", "/admin/customers",
                 f"/admin/customers/{cust_id}", "/admin/conversations", f"/admin/conversations/{conv_id}",
                 "/admin/outreach"]:
        r = client.get(path)
        assert r.status_code == 200, path
    assert "Dave" in client.get(f"/admin/conversations/{conv_id}").text


def test_dashboard_import_and_outreach_approval(client, session, deps):
    login(client)
    csv = (
        "Date,Customer Phone,Customer Name,Item,Transaction ID\n"
        + "\n".join(f"2026-0{m}-0{d} 18:00,5551239999,Jo Park,Large Pepperoni Pizza,T{m}{d}" for m, d in [(7, 1), (7, 8), (7, 9)])
    )
    r = client.post("/admin/import", files={"file": ("orders.csv", csv, "text/csv")}, data={"source": "square", "consent": "on"})
    assert "orders imported" in r.text and session.query(Order).count() == 3

    cust = session.query(Customer).one()
    client.post(f"/admin/customers/{cust.id}/reach-out", data={"mode": "approve"})
    draft = session.query(Outreach).one()
    assert draft.status == "pending_approval"
    assert "Jo" in client.get("/admin/outreach").text
    client.post(f"/admin/outreach/{draft.id}/approve", data={"message": "Jo! Marco from Tony's. Pepperoni tonight?"})
    session.expire_all()
    assert session.get(Outreach, draft.id).status == "sent"
    assert deps.messenger.sent[-1] == ("+15551239999", "Jo! Marco from Tony's. Pepperoni tonight?")


def test_dashboard_takeover_and_staff_reply(client, session, settings, deps):
    make_customer(session, settings)
    client.post("/webhook/incoming", json=sendblue_inbound("hi"))
    login(client)
    conv_id = session.query(Conversation).one().id
    client.post(f"/admin/conversations/{conv_id}/reply", data={"text": "Hey Dave, it's Tony himself!"})
    assert deps.messenger.sent[-1][1] == "Hey Dave, it's Tony himself!"
    before = len(deps.messenger.sent)
    client.post("/webhook/incoming", json=sendblue_inbound("oh hey tony", handle="m9"))
    assert len(deps.messenger.sent) == before  # Bruno stays out of it
    client.post(f"/admin/conversations/{conv_id}/takeover", data={"on": "0"})
    session.expire_all()
    assert session.get(Conversation, conv_id).human_takeover is False
