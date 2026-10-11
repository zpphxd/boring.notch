"""Payment step. Charges the customer's tokenized card / Apple Pay on file.

The mock provider is what the MVP runs on; a real processor (Stripe, Square
Payments) slots in behind the same two methods.
"""

import hashlib
from dataclasses import dataclass
from typing import Protocol

from bruno.config import Settings
from bruno.models import Customer


@dataclass
class PaymentResult:
    status: str  # captured | pay_at_pickup | failed
    reference: str | None = None
    method: str = "pay at pickup"
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("captured", "pay_at_pickup")


class PaymentProvider(Protocol):
    def charge(self, customer: Customer, amount_cents: int, idempotency_key: str) -> PaymentResult: ...

    def refund(self, reference: str) -> None: ...


def payment_method_label(customer: Customer) -> str:
    if customer.payment_token:
        return customer.card_label or "card on file"
    return "pay at pickup"


class MockPaymentProvider:
    def __init__(self) -> None:
        self.charges: dict[str, int] = {}
        self.refunds: list[str] = []

    def charge(self, customer: Customer, amount_cents: int, idempotency_key: str) -> PaymentResult:
        if not customer.payment_token:
            return PaymentResult(status="pay_at_pickup")
        ref = "mock_ch_" + hashlib.sha1(idempotency_key.encode()).hexdigest()[:16]
        self.charges[ref] = amount_cents
        return PaymentResult(status="captured", reference=ref, method=payment_method_label(customer))

    def refund(self, reference: str) -> None:
        self.refunds.append(reference)


def build_payments(settings: Settings) -> PaymentProvider:
    if settings.payment_provider == "mock":
        return MockPaymentProvider()
    raise ValueError(f"Unknown PAYMENT_PROVIDER: {settings.payment_provider}")
