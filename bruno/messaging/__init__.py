"""Text / iMessage channel: outbound senders and inbound webhook parsing."""

import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from bruno.config import Settings

log = logging.getLogger(__name__)


def normalize_phone(raw: str) -> str:
    """Best-effort E.164 normalization (US default)."""
    raw = (raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if digits else raw


class MessagingError(Exception):
    pass


class Messenger(Protocol):
    def send(self, to: str, text: str) -> str | None:
        """Send a text; returns the provider's message id when available."""
        ...


@dataclass
class ConsoleMessenger:
    """Prints outbound texts to the terminal (and keeps them for tests)."""

    echo: bool = True
    sent: list[tuple[str, str]] = field(default_factory=list)

    def send(self, to: str, text: str) -> str | None:
        self.sent.append((to, text))
        if self.echo:
            print(f"\n📱  → {to}\n{text}\n")
        return None


class SendblueMessenger:
    """iMessage (with SMS fallback) via the Sendblue API."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not (settings.sendblue_api_key_id and settings.sendblue_api_secret_key):
            raise ValueError("Sendblue requires SENDBLUE_API_KEY_ID and SENDBLUE_API_SECRET_KEY")
        self.settings = settings
        self.client = client or httpx.Client(base_url=settings.sendblue_base_url, timeout=15)
        self.headers = {
            "sb-api-key-id": settings.sendblue_api_key_id,
            "sb-api-secret-key": settings.sendblue_api_secret_key,
            "Content-Type": "application/json",
        }

    def send(self, to: str, text: str) -> str | None:
        body: dict = {"number": to, "content": text}
        if self.settings.sendblue_from_number:
            body["from_number"] = self.settings.sendblue_from_number
        if self.settings.sendblue_status_callback_url:
            body["status_callback"] = self.settings.sendblue_status_callback_url
        try:
            resp = self.client.post("/api/send-message", json=body, headers=self.headers)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise MessagingError(f"Sendblue send failed: {exc}") from exc
        data = resp.json()
        if str(data.get("status", "")).upper() == "ERROR":
            raise MessagingError(f"Sendblue error: {data.get('error_message')}")
        return data.get("message_handle")


@dataclass
class InboundText:
    from_number: str
    content: str
    message_id: str | None
    media_url: str | None = None


def parse_sendblue_inbound(payload: dict) -> InboundText | None:
    """Sendblue posts both inbound messages and outbound status updates to
    webhooks; only customer-sent messages produce an InboundText."""
    if payload.get("is_outbound"):
        return None
    number = payload.get("from_number") or payload.get("number")
    content = (payload.get("content") or "").strip()
    media = payload.get("media_url") or None
    if not number or not (content or media):
        return None
    return InboundText(
        from_number=normalize_phone(number),
        content=content or "[sent an attachment]",
        message_id=payload.get("message_handle"),
        media_url=media,
    )


def build_messenger(settings: Settings) -> Messenger:
    if settings.messaging_provider == "sendblue":
        return SendblueMessenger(settings)
    if settings.messaging_provider == "console":
        return ConsoleMessenger()
    raise ValueError(f"Unknown MESSAGING_PROVIDER: {settings.messaging_provider}")
