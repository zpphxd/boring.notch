from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from bruno.api import create_app
from bruno.models import Conversation, Customer, Order
from tests.conftest import DAVE_PHONE, make_customer


@pytest.fixture
def client(deps, session_factory):
    return TestClient(create_app(deps, session_factory))


def sendblue_inbound(content, handle="m1", number=DAVE_PHONE):
    return {
        "accountEmail": "demo@example.com",
        "content": content,
        "is_outbound": False,
        "status": "RECEIVED",
        "message_handle": handle,
        "from_number": number,
        "number": number,
        "to_number": "+15550000000",
        "service": "iMessage",
    }


def test_health(client):
    assert client.get("/health").json()["llm"] == "mock"


def test_inbound_text_webhook_runs_agent(client, deps, session, settings):
    make_customer(session, settings)
    r = client.post("/webhook/incoming", json=sendblue_inbound("can I get my usual"))
    assert r.json() == {"status": "accepted"}
    to, reply = deps.messenger.sent[-1]
    assert to == DAVE_PHONE
    assert "Reply YES to place it" in reply

    client.post("/webhook/incoming", json=sendblue_inbound("yes", handle="m2"))
    assert "You're all set, Dave!" in deps.messenger.sent[-1][1]
    assert len(deps.pos.orders) == 1


def test_outbound_status_payloads_are_ignored(client, deps):
    r = client.post("/webhook/incoming", json={**sendblue_inbound("hi"), "is_outbound": True})
    assert r.json() == {"status": "ignored"}
    assert deps.messenger.sent == []


def test_webhook_secret(deps, session_factory):
    d = replace(deps, settings=deps.settings.model_copy(update={"webhook_secret": "s3cret"}))
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


def test_retell_unknown_caller(client):
    r = client.post("/voice/retell/inbound", json={"call_inbound": {"from_number": "+15550001111"}})
    assert r.json()["call_inbound"]["dynamic_variables"]["greeting"].startswith("Hey, thanks for calling Tony's")


def test_retell_functions_place_order(client, deps, session, settings):
    make_customer(session, settings)
    call = {"from_number": DAVE_PHONE, "call_id": "c1"}
    r = client.post(
        "/voice/retell/functions/update_cart",
        json={
            "name": "update_cart",
            "call": call,
            "args": {"items": [{"item_id": "pizza", "size": "large", "quantity": 1, "modifiers": ["pepperoni"]}]},
        },
    )
    body = r.json()
    assert body["ok"] and "Large Pizza w/ Pepperoni" in body["summary"]

    r = client.post(
        "/voice/retell/functions/place_order",
        json={"name": "place_order", "call": call, "args": {"customer_confirmed": True}},
    )
    assert r.json()["ok"]
    assert deps.pos.orders[0].channel == "voice"


def test_pos_ingest_updates_cadence(client, session, settings):
    make_customer(session, settings)  # drifting
    r = client.post(
        "/webhook/pos/order",
        json={
            "phone": DAVE_PHONE,
            "external_id": "sq-123",
            "items": [{"item_id": "pizza", "size": "large", "modifiers": ["pepperoni"]}],
        },
    )
    assert r.json()["drifting"] is False
    assert client.post(
        "/webhook/pos/order", json={"phone": DAVE_PHONE, "external_id": "sq-123", "items": []}
    ).json() == {"status": "duplicate"}
    session.expire_all()
    assert session.query(Order).filter_by(external_id="sq-123").count() == 1
    assert session.query(Customer).count() == 1
    assert session.query(Conversation).count() == 0
