import base64
import hashlib
import hmac
import json

import httpx

from bruno.delivery import DoorDashDrive, doordash_jwt
from tests.conftest import make_settings

SECRET = base64.urlsafe_b64encode(b"super-secret-signing-key").decode().rstrip("=")


def _b64d(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_jwt_format_and_signature():
    token = doordash_jwt("dev-1", "key-1", SECRET, now=1_700_000_000)
    header, payload, sig = token.split(".")
    assert json.loads(_b64d(header)) == {"alg": "HS256", "typ": "JWT", "dd-ver": "DD-JWT-V1"}
    claims = json.loads(_b64d(payload))
    assert claims == {"aud": "doordash", "iss": "dev-1", "kid": "key-1", "iat": 1_700_000_000, "exp": 1_700_000_300}
    expected = hmac.new(b"super-secret-signing-key", f"{header}.{payload}".encode(), hashlib.sha256).digest()
    assert _b64d(sig) == expected


def test_quote_and_create_requests():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.url.path, body, request.headers["authorization"]))
        if request.url.path.endswith("/quotes"):
            return httpx.Response(200, json={"fee": 975, "dropoff_time_estimated": "2026-10-09T23:15:00Z"})
        return httpx.Response(200, json={"external_delivery_id": body["external_delivery_id"],
                                         "tracking_url": "https://track/x", "dropoff_time_estimated": "2026-10-09T23:20:00Z"})

    s = make_settings(doordash_developer_id="d", doordash_key_id="k", doordash_signing_secret=SECRET,
                      restaurant_address="1 Pizza Way, Brooklyn NY", restaurant_phone="+15550009999")
    drive = DoorDashDrive(s, client=httpx.Client(base_url="https://openapi.doordash.com", transport=httpx.MockTransport(handler)))
    q = drive.quote(external_id="q1", address="12 Main St", phone="+15551230001", name="Dave Rossi", order_value_cents=2214)
    assert q.fee_cents == 975 and q.dropoff_eta.hour == 23
    from datetime import datetime, timezone

    r = drive.create(external_id="o1", address="12 Main St", phone="+15551230001", name="Dave Rossi",
                     order_value_cents=2214, pickup_time=datetime(2026, 10, 9, 22, 30, tzinfo=timezone.utc),
                     items_summary="large pepperoni pizza")
    assert r.tracking_url == "https://track/x"
    path, body, auth = seen[1]
    assert path == "/drive/v2/deliveries" and auth.startswith("Bearer ")
    assert body["pickup_address"] == "1 Pizza Way, Brooklyn NY" and body["dropoff_contact_given_name"] == "Dave"
    assert body["order_value"] == 2214 and "pepperoni" in body["pickup_instructions"]
