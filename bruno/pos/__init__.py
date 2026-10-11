"""POS fulfillment layer: strict transactional order JSON -> POS / kitchen."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel

from bruno.config import Settings
from bruno.menu import PricedLine

log = logging.getLogger(__name__)


class POSCustomer(BaseModel):
    name: str
    phone: str


class POSPayment(BaseModel):
    status: str  # captured | pay_at_pickup
    method: str
    reference: str | None = None


class POSOrder(BaseModel):
    """The canonical order both channels hand to the POS."""

    external_id: str  # idempotency key, e.g. "bruno-conv-12"
    restaurant: str
    channel: str  # text | voice
    customer: POSCustomer
    pickup_at: datetime  # timezone-aware local time
    line_items: list[PricedLine]
    subtotal_cents: int
    tax_cents: int
    total_cents: int
    payment: POSPayment
    created_at: datetime


class POSResult(BaseModel):
    pos_order_id: str
    raw: dict = {}


class POSError(Exception):
    pass


class POSClient(Protocol):
    def inject(self, order: POSOrder) -> POSResult: ...


def render_ticket(order: POSOrder) -> str:
    width = 44
    rule = "=" * width
    lines = [
        rule,
        f"{order.restaurant.upper():^{width}}",
        f"{'*** BRUNO ' + order.channel.upper() + ' ORDER ***':^{width}}",
        rule,
        f"Ticket:  {order.external_id}",
        f"Name:    {order.customer.name}",
        f"Phone:   {order.customer.phone}",
        f"Pickup:  {order.pickup_at.strftime('%a %b %d, %I:%M %p')}",
        "-" * width,
    ]
    for li in order.line_items:
        size = f"{li.size.upper()} " if li.size else ""
        head = f"{li.quantity}x {size}{li.name}"
        price = f"${li.line_total_cents / 100:.2f}"
        lines.append(f"{head[: width - len(price) - 1]:<{width - len(price)}}{price}")
        for mod in li.modifiers:
            lines.append(f"    + {mod.name}")
        if li.notes:
            lines.append(f"    ! {li.notes}")
    lines += [
        "-" * width,
        f"{'Subtotal':<{width - 10}}{order.subtotal_cents / 100:>10.2f}",
        f"{'Tax':<{width - 10}}{order.tax_cents / 100:>10.2f}",
        f"{'TOTAL':<{width - 10}}{order.total_cents / 100:>10.2f}",
        f"Payment: {order.payment.method} ({order.payment.status.replace('_', ' ')})",
        rule,
    ]
    return "\n".join(lines)


class ConsolePOS:
    """Terminal 'kitchen printer' for the MVP: prints the ticket and the exact JSON
    payload, and appends it to a JSONL log."""

    def __init__(self, log_path: str | None = None, echo: bool = True):
        self.log_path = Path(log_path) if log_path else None
        self.echo = echo
        self.orders: list[POSOrder] = []

    def inject(self, order: POSOrder) -> POSResult:
        self.orders.append(order)
        payload = order.model_dump(mode="json")
        if self.echo:
            print("\n🍕  KITCHEN TICKET RECEIVED\n" + render_ticket(order))
            print("POS payload:\n" + json.dumps(payload, indent=2) + "\n")
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as fh:
                fh.write(json.dumps(payload) + "\n")
        return POSResult(pos_order_id=f"console-{order.external_id}", raw=payload)


class SquarePOS:
    """Square Orders API (CreateOrder) with a scheduled PICKUP fulfillment.

    Uses ad-hoc line items so it works without catalog mapping; swap in
    catalog_object_id per item once the menu is synced with Square's catalog.
    """

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not (settings.square_access_token and settings.square_location_id):
            raise ValueError("Square requires SQUARE_ACCESS_TOKEN and SQUARE_LOCATION_ID")
        self.location_id = settings.square_location_id
        self.client = client or httpx.Client(base_url=settings.square_base_url, timeout=15)
        self.headers = {
            "Authorization": f"Bearer {settings.square_access_token}",
            "Square-Version": settings.square_api_version,
            "Content-Type": "application/json",
        }

    def build_request(self, order: POSOrder) -> dict:
        def money(cents: int) -> dict:
            return {"amount": cents, "currency": "USD"}

        line_items = []
        for li in order.line_items:
            name = f"{li.size.capitalize()} {li.name}" if li.size else li.name
            line_items.append(
                {
                    "name": name,
                    "quantity": str(li.quantity),
                    "base_price_money": money(li.unit_price_cents - sum(m.price_cents for m in li.modifiers)),
                    "modifiers": [{"name": m.name, "base_price_money": money(m.price_cents)} for m in li.modifiers],
                    **({"note": li.notes} if li.notes else {}),
                }
            )
        tax_pct = round(order.tax_cents / order.subtotal_cents * 100, 3) if order.subtotal_cents else 0
        return {
            "idempotency_key": order.external_id,
            "order": {
                "location_id": self.location_id,
                "reference_id": order.external_id,
                "source": {"name": f"Bruno ({order.channel})"},
                "line_items": line_items,
                "taxes": [{"uid": "sales-tax", "name": "Sales Tax", "percentage": str(tax_pct), "scope": "ORDER"}],
                "fulfillments": [
                    {
                        "type": "PICKUP",
                        "state": "PROPOSED",
                        "pickup_details": {
                            "recipient": {
                                "display_name": order.customer.name,
                                "phone_number": order.customer.phone,
                            },
                            "schedule_type": "SCHEDULED",
                            "pickup_at": order.pickup_at.isoformat(),
                            "note": f"Payment: {order.payment.method} ({order.payment.status})",
                        },
                    }
                ],
            },
        }

    def inject(self, order: POSOrder) -> POSResult:
        try:
            resp = self.client.post("/v2/orders", json=self.build_request(order), headers=self.headers)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise POSError(f"Square CreateOrder failed: {exc}") from exc
        data = resp.json()
        return POSResult(pos_order_id=data["order"]["id"], raw=data)


def build_pos(settings: Settings) -> POSClient:
    if settings.pos_provider == "console":
        return ConsolePOS(settings.kitchen_ticket_log)
    if settings.pos_provider == "square":
        return SquarePOS(settings)
    raise ValueError(f"Unknown POS_PROVIDER: {settings.pos_provider} (toast/clover adapters not built yet)")
