"""Delivery.

* DoorDash Drive (https://developer.doordash.com/docs/drive): the restaurant's
  own order, delivered by a DoorDash courier. Bruno gets a quote to check the
  address is deliverable, then creates the delivery when the order is placed.
* Marketplace hand-off: DoorDash has no public API for placing marketplace
  orders, so when Drive isn't set up Bruno sends the customer the restaurant's
  own DoorDash store link instead.
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx

from bruno.config import Settings

log = logging.getLogger(__name__)


class DeliveryError(Exception):
    pass


@dataclass
class DeliveryQuote:
    fee_cents: int  # what DoorDash charges the restaurant
    dropoff_eta: datetime | None
    external_id: str


@dataclass
class DeliveryResult:
    delivery_id: str
    tracking_url: str | None
    dropoff_eta: datetime | None


class DeliveryProvider(Protocol):
    def quote(self, *, external_id: str, address: str, phone: str, name: str, order_value_cents: int) -> DeliveryQuote: ...

    def create(
        self, *, external_id: str, address: str, phone: str, name: str, order_value_cents: int,
        pickup_time: datetime, items_summary: str,
    ) -> DeliveryResult: ...

    def cancel(self, delivery_id: str) -> None: ...


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def doordash_jwt(developer_id: str, key_id: str, signing_secret: str, now: float | None = None) -> str:
    """HS256 JWT in DoorDash's DD-JWT-V1 format."""
    now = int(now or time.time())
    header = {"alg": "HS256", "typ": "JWT", "dd-ver": "DD-JWT-V1"}
    payload = {"aud": "doordash", "iss": developer_id, "kid": key_id, "iat": now, "exp": now + 300}
    signing_input = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}." \
                    f"{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    secret = base64.urlsafe_b64decode(signing_secret + "=" * (-len(signing_secret) % 4))
    sig = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(sig)}"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class DoorDashDrive:
    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not settings.drive_ready:
            raise ValueError("DoorDash Drive needs developer ID, key ID, signing secret and a pickup address")
        self.settings = settings
        self.client = client or httpx.Client(base_url=settings.doordash_base_url, timeout=20)

    def _headers(self) -> dict:
        s = self.settings
        token = doordash_jwt(s.doordash_developer_id, s.doordash_key_id, s.doordash_signing_secret)
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _body(self, external_id: str, address: str, phone: str, name: str, order_value_cents: int) -> dict:
        s = self.settings
        first, _, last = name.partition(" ")
        return {
            "external_delivery_id": external_id,
            "pickup_address": s.restaurant_address,
            "pickup_business_name": s.restaurant_name,
            "pickup_phone_number": s.restaurant_phone or s.owner_phone,
            "dropoff_address": address,
            "dropoff_phone_number": phone,
            "dropoff_contact_given_name": first or "Customer",
            "dropoff_contact_family_name": last,
            "order_value": order_value_cents,
        }

    def _post(self, path: str, body: dict) -> dict:
        try:
            resp = self.client.post(path, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise DeliveryError(f"DoorDash unreachable: {exc}") from exc
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("message") or resp.text
            except ValueError:
                detail = resp.text
            raise DeliveryError(f"DoorDash {resp.status_code}: {detail}")
        return resp.json()

    def quote(self, *, external_id: str, address: str, phone: str, name: str, order_value_cents: int) -> DeliveryQuote:
        data = self._post("/drive/v2/quotes", self._body(external_id, address, phone, name, order_value_cents))
        return DeliveryQuote(
            fee_cents=int(data.get("fee", 0)),
            dropoff_eta=_parse_ts(data.get("dropoff_time_estimated")),
            external_id=external_id,
        )

    def create(
        self, *, external_id: str, address: str, phone: str, name: str, order_value_cents: int,
        pickup_time: datetime, items_summary: str,
    ) -> DeliveryResult:
        body = self._body(external_id, address, phone, name, order_value_cents)
        body["pickup_time"] = pickup_time.isoformat()
        body["pickup_instructions"] = f"Order for {name}: {items_summary}"[:500]
        data = self._post("/drive/v2/deliveries", body)
        return DeliveryResult(
            delivery_id=data.get("external_delivery_id", external_id),
            tracking_url=data.get("tracking_url"),
            dropoff_eta=_parse_ts(data.get("dropoff_time_estimated")),
        )

    def cancel(self, delivery_id: str) -> None:
        try:
            self.client.put(f"/drive/v2/deliveries/{delivery_id}/cancel", headers=self._headers())
        except httpx.HTTPError as exc:
            log.error("DoorDash cancel failed for %s: %s", delivery_id, exc)


def build_delivery(settings: Settings) -> DeliveryProvider | None:
    return DoorDashDrive(settings) if settings.drive_ready else None


def marketplace_links(settings: Settings) -> list[tuple[str, str]]:
    return [("DoorDash", settings.doordash_store_url)] if settings.doordash_store_url else []
