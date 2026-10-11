import hashlib
import hmac
import json
import time
from dataclasses import replace
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient

from bruno import outreach
from bruno.agent import Bruno
from bruno.api import create_app
from bruno.fsm import State
from bruno.models import Conversation, Customer
from bruno.payments import PaymentLink, SavedCard, StripePayments, verify_stripe_signature
from tests.conftest import DAVE_PHONE, FRIDAY_430PM, make_customer, make_settings
from tests.test_agent import FakeDrive

WHSEC = "whsec_test"


def sign(payload: bytes, secret: str = WHSEC, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def test_signature_verification():
    body = b'{"x":1}'
    assert verify_stripe_signature(body, sign(body), WHSEC)
    assert not verify_stripe_signature(body, sign(body, "other"), WHSEC)
    assert not verify_stripe_signature(body, sign(body, ts=int(time.time()) - 3600), WHSEC)
    assert not verify_stripe_signature(body, "garbage", WHSEC)


class FakeStripe:
    supports_links = True

    def __init__(self):
        self.links, self.charges, self.refunds = [], [], []

    def has_saved_card(self, customer):
        return bool(customer.payment_token and customer.payment_token.startswith("cus_"))

    def charge(self, customer, amount_cents, idempotency_key):
        from bruno.payments import PaymentResult

        if not self.has_saved_card(customer):
            return PaymentResult(status="pay_at_pickup")
        self.charges.append(amount_cents)
        return PaymentResult(status="captured", reference="pi_saved", method=customer.card_label)

    def refund(self, reference):
        self.refunds.append(reference)

    def payment_link(self, customer, amount_cents, description, conversation_id):
        self.links.append((amount_cents, conversation_id))
        return PaymentLink(url=f"https://checkout.stripe.com/c/pay/cs_{conversation_id}", reference=f"cs_{conversation_id}")

    def saved_card_from(self, payment_intent_id):
        return SavedCard(token="cus_123:pm_456", label="Amex •1005")


def test_delivery_without_saved_card_uses_pay_link_then_webhook_places_order(session_factory, deps, settings):
    stripe = FakeStripe()
    d = replace(deps, payments=stripe, delivery=FakeDrive(),
                settings=make_settings(stripe_webhook_secret=WHSEC, public_base_url="https://bruno.example"))
    session = session_factory()
    dave = make_customer(session, settings, card=False)
    client = TestClient(create_app(d, session_factory))
    agent = Bruno(session, d)
    outreach.reach_out(session, d, dave, FRIDAY_430PM, mode="auto")
    session.commit()

    out = agent.handle_inbound_text(DAVE_PHONE, "deliver it to 12 Main St please")
    assert out.state == State.CONFIRMING_PAYMENT and "secure link" in out.reply
    out = agent.handle_inbound_text(DAVE_PHONE, "yes")
    assert out.state == State.AWAITING_PAYMENT
    assert "https://checkout.stripe.com/c/pay/cs_" in out.reply and deps.pos.orders == []
    assert stripe.links[0][0] == 2713  # food + tax + delivery

    out = agent.handle_inbound_text(DAVE_PHONE, "ok paying now")
    assert out.state == State.AWAITING_PAYMENT and "checkout.stripe.com" in out.reply
    session.commit()

    conv_id = out.conversation_id
    event = {"type": "checkout.session.completed",
             "data": {"object": {"payment_status": "paid", "payment_intent": "pi_1",
                                 "metadata": {"conversation_id": str(conv_id)}}}}
    body = json.dumps(event).encode()
    assert client.post("/webhook/stripe", content=body, headers={"stripe-signature": "t=1,v1=bad"}).status_code == 400
    r = client.post("/webhook/stripe", content=body, headers={"stripe-signature": sign(body)})
    assert r.json() == {"status": "ok", "placed": True}
    assert deps.pos.orders[0].payment.reference == "pi_1" and deps.pos.orders[0].fulfillment == "delivery"
    assert "DoorDash driver" in deps.messenger.sent[-2][1] or "DoorDash driver" in deps.messenger.sent[-1][1]

    session.expire_all()
    customer = session.get(Customer, dave.id)
    assert customer.payment_token == "cus_123:pm_456" and customer.card_label == "Amex •1005"
    assert session.get(Conversation, conv_id).state == State.COMPLETED
    # Webhook retries don't double-place
    assert client.post("/webhook/stripe", content=body, headers={"stripe-signature": sign(body)}).json()["placed"] is False
    assert len(deps.pos.orders) == 1 and stripe.refunds == []
    session.close()


def test_pickup_without_card_is_pay_at_pickup_by_default(session, deps, settings):
    d = replace(deps, payments=FakeStripe())
    dave = make_customer(session, settings, card=False)
    agent = Bruno(session, d, clock=lambda: FRIDAY_430PM)
    outreach.reach_out(session, d, dave, FRIDAY_430PM, mode="auto")
    agent.handle_inbound_text(DAVE_PHONE, "yes")
    assert agent.handle_inbound_text(DAVE_PHONE, "yes").state == State.COMPLETED


def test_stripe_requests():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        calls.append((request.url.path, form, request.headers.get("idempotency-key")))
        if request.url.path == "/v1/payment_intents":
            return httpx.Response(200, json={"id": "pi_9", "status": "succeeded"})
        if request.url.path == "/v1/checkout/sessions":
            return httpx.Response(200, json={"id": "cs_9", "url": "https://checkout.stripe.com/x"})
        return httpx.Response(200, json={})

    s = make_settings(stripe_secret_key="sk_test_1", public_base_url="https://bruno.example")
    stripe = StripePayments(s, client=httpx.Client(base_url="https://api.stripe.com", transport=httpx.MockTransport(handler)))
    saved = Customer(phone_number="+1", payment_token="cus_1:pm_2", card_label="Visa •4242")
    res = stripe.charge(saved, 2214, "bruno-conv-5")
    assert res.status == "captured" and res.reference == "pi_9"
    path, form, idem = calls[0]
    assert form["customer"] == "cus_1" and form["payment_method"] == "pm_2" and form["off_session"] == "true"
    assert form["amount"] == "2214" and idem == "charge-bruno-conv-5-2214"

    link = stripe.payment_link(Customer(phone_number="+1"), 2713, "Tony's: pizza", 5)
    assert link.url == "https://checkout.stripe.com/x"
    form = calls[1][1]
    assert form["success_url"] == "https://bruno.example/paid" and form["customer_creation"] == "always"
    assert form["payment_intent_data[setup_future_usage]"] == "off_session" and form["metadata[conversation_id]"] == "5"
    # Demo / non-Stripe tokens are never charged
    assert stripe.charge(Customer(phone_number="+1", payment_token="tok_demo"), 100, "x").status == "pay_at_pickup"
