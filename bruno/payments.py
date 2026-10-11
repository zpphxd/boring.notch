"""Payments.

* Stripe (real): charges a saved card off-session for one-text reorders. When
  a customer has no saved card, Bruno texts a Stripe Checkout link; the order
  goes to the kitchen when Stripe's webhook confirms payment, and the card is
  saved for next time.
* Mock: for demos and tests. "Charges" customers who have a demo card token.

Without a saved card (or a payment link), pickup orders are pay-at-pickup.
"""

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from bruno.config import Settings
from bruno.models import Customer

log = logging.getLogger(__name__)


@dataclass
class PaymentResult:
    status: str  # captured | pay_at_pickup | failed
    reference: str | None = None
    method: str = "pay at pickup"
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("captured", "pay_at_pickup")


@dataclass
class PaymentLink:
    url: str
    reference: str


@dataclass
class SavedCard:
    token: str  # "cus_...:pm_..."
    label: str  # "Visa •4242"


class PaymentError(Exception):
    pass


class PaymentProvider(Protocol):
    supports_links: bool

    def charge(self, customer: Customer, amount_cents: int, idempotency_key: str) -> PaymentResult: ...

    def refund(self, reference: str) -> None: ...

    def has_saved_card(self, customer: Customer) -> bool: ...

    def payment_link(self, customer: Customer, amount_cents: int, description: str, conversation_id: int) -> PaymentLink: ...


def payment_method_label(customer: Customer) -> str:
    if customer.payment_token:
        return customer.card_label or "card on file"
    return "pay at pickup"


class MockPaymentProvider:
    supports_links = False

    def __init__(self) -> None:
        self.charges: dict[str, int] = {}
        self.refunds: list[str] = []

    def has_saved_card(self, customer: Customer) -> bool:
        return bool(customer.payment_token)

    def charge(self, customer: Customer, amount_cents: int, idempotency_key: str) -> PaymentResult:
        if not customer.payment_token:
            return PaymentResult(status="pay_at_pickup")
        ref = "mock_ch_" + hashlib.sha1(idempotency_key.encode()).hexdigest()[:16]
        self.charges[ref] = amount_cents
        return PaymentResult(status="captured", reference=ref, method=payment_method_label(customer))

    def refund(self, reference: str) -> None:
        self.refunds.append(reference)

    def payment_link(self, customer: Customer, amount_cents: int, description: str, conversation_id: int) -> PaymentLink:
        raise PaymentError("payment links need Stripe")


class StripePayments:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not settings.stripe_secret_key:
            raise ValueError("Stripe needs STRIPE_SECRET_KEY")
        self.settings = settings
        self.client = client or httpx.Client(
            base_url="https://api.stripe.com", timeout=20, auth=(settings.stripe_secret_key, "")
        )
        # Checkout needs somewhere to send the customer afterwards.
        self.supports_links = bool(settings.public_base_url)

    def _request(self, method: str, path: str, data: dict | None = None, idempotency_key: str | None = None) -> dict:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        try:
            resp = self.client.request(method, path, data=data, headers=headers)
        except httpx.HTTPError as exc:
            raise PaymentError(f"Stripe unreachable: {exc}") from exc
        body = resp.json()
        if resp.status_code >= 400:
            raise PaymentError(body.get("error", {}).get("message", resp.text))
        return body

    @staticmethod
    def _split(token: str | None) -> tuple[str, str] | None:
        if token and token.startswith("cus_") and ":pm_" in token:
            cus, pm = token.split(":", 1)
            return cus, pm
        return None

    def has_saved_card(self, customer: Customer) -> bool:
        return self._split(customer.payment_token) is not None

    def charge(self, customer: Customer, amount_cents: int, idempotency_key: str) -> PaymentResult:
        saved = self._split(customer.payment_token)
        if not saved:
            return PaymentResult(status="pay_at_pickup")
        try:
            intent = self._request(
                "POST", "/v1/payment_intents",
                {
                    "amount": amount_cents, "currency": "usd", "customer": saved[0], "payment_method": saved[1],
                    "off_session": "true", "confirm": "true",
                    "description": f"{self.settings.restaurant_name} order {idempotency_key}",
                    "metadata[order]": idempotency_key,
                },
                idempotency_key=f"charge-{idempotency_key}-{amount_cents}",
            )
        except PaymentError as exc:
            return PaymentResult(status="failed", error=str(exc))
        if intent.get("status") != "succeeded":
            return PaymentResult(status="failed", error=f"payment {intent.get('status')}")
        return PaymentResult(status="captured", reference=intent["id"], method=payment_method_label(customer))

    def refund(self, reference: str) -> None:
        try:
            self._request("POST", "/v1/refunds", {"payment_intent": reference}, idempotency_key=f"refund-{reference}")
        except PaymentError as exc:
            log.error("Stripe refund failed for %s: %s", reference, exc)

    def payment_link(self, customer: Customer, amount_cents: int, description: str, conversation_id: int) -> PaymentLink:
        data = {
            "mode": "payment",
            "line_items[0][quantity]": 1,
            "line_items[0][price_data][currency]": "usd",
            "line_items[0][price_data][unit_amount]": amount_cents,
            "line_items[0][price_data][product_data][name]": description[:250] or "Your order",
            "success_url": self.settings.public_base_url.rstrip("/") + "/paid",
            "metadata[conversation_id]": str(conversation_id),
            "payment_intent_data[setup_future_usage]": "off_session",
            "payment_intent_data[metadata][conversation_id]": str(conversation_id),
        }
        saved = self._split(customer.payment_token)
        if saved:
            data["customer"] = saved[0]
        else:
            data["customer_creation"] = "always"
        session = self._request(
            "POST", "/v1/checkout/sessions", data, idempotency_key=f"link-{conversation_id}-{amount_cents}"
        )
        return PaymentLink(url=session["url"], reference=session["id"])

    def saved_card_from(self, payment_intent_id: str) -> SavedCard | None:
        intent = self._request("GET", f"/v1/payment_intents/{payment_intent_id}?expand[]=payment_method")
        pm = intent.get("payment_method") or {}
        if not intent.get("customer") or not isinstance(pm, dict) or not pm.get("id"):
            return None
        card = pm.get("card") or {}
        label = f"{(card.get('brand') or 'card').title()} •{card.get('last4', '')}".strip()
        return SavedCard(token=f"{intent['customer']}:{pm['id']}", label=label)


def verify_stripe_signature(payload: bytes, header: str, secret: str, tolerance: int = 300, now: float | None = None) -> bool:
    """Stripe-Signature: t=timestamp,v1=hex(hmac_sha256(secret, f"{t}.{payload}"))."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
        ts = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if abs((now or time.time()) - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    signatures = [v for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p) if k == "v1"]
    return any(hmac.compare_digest(expected, sig) for sig in signatures)


def build_payments(settings: Settings) -> PaymentProvider:
    provider = settings.payment_provider
    if provider == "auto":
        provider = "stripe" if settings.stripe_secret_key else "mock"
    if provider == "stripe":
        return StripePayments(settings)
    if provider == "mock":
        return MockPaymentProvider()
    raise ValueError(f"Unknown PAYMENT_PROVIDER: {settings.payment_provider}")
