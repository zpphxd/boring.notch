"""Text / iMessage channel: outbound senders and inbound webhook parsing."""

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote

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

    def typing(self, to: str) -> None:
        """Show the '...' typing bubble (best effort)."""
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

    def typing(self, to: str) -> None:
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

    def typing(self, to: str) -> None:
        body: dict = {"number": to}
        if self.settings.sendblue_from_number:
            body["from_number"] = self.settings.sendblue_from_number
        try:
            self.client.post("/api/send-typing-indicator", json=body, headers=self.headers, timeout=5)
        except httpx.HTTPError:
            pass  # cosmetic only


class BlueBubblesMessenger:
    """iMessage through a self-hosted BlueBubbles Server (a Mac signed into Messages).

    REST API (server source: github.com/BlueBubblesApp/bluebubbles-server,
    packages/server/src/server/api/http/api/v1/):
      * auth: every request carries the server password as ``?password=``
        (the server also accepts ``guid`` / ``token``).
      * send: ``POST /api/v1/message/text`` with ``chatGuid``, ``message``,
        ``method`` ("apple-script" or "private-api") and ``tempGuid`` (required
        for AppleScript; also the server's de-dupe key, so it's unique per send).
      * typing: ``POST /api/v1/chat/:chatGuid/typing`` (Private API only).
      * new chat: ``POST /api/v1/chat/new`` with ``addresses`` + ``message``.
      * health: ``GET /api/v1/ping`` and ``GET /api/v1/server/info``.
    Responses look like ``{"status": 200, "message": "...", "data": ...}``.
    """

    # Sends wait until the message shows up in the Mac's Messages database
    # (up to 60s for the Private API), so allow a slow response.
    TIMEOUT = httpx.Timeout(75.0, connect=10.0)

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        if not (settings.bluebubbles_url and settings.bluebubbles_password):
            raise ValueError("BlueBubbles requires BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD")
        self.base_url = bluebubbles_base_url(settings.bluebubbles_url)
        self.password = settings.bluebubbles_password
        self.private_api = settings.bluebubbles_private_api
        self.client = client or httpx.Client(timeout=self.TIMEOUT)

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        kwargs: dict = {"params": {"password": self.password}}
        if body is not None:
            kwargs["json"] = body
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            resp = self.client.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise MessagingError(f"BlueBubbles server unreachable at {self.base_url}: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if resp.status_code >= 400:
            raise BlueBubblesError(resp.status_code, _bluebubbles_error_text(data) or resp.reason_phrase)
        return data

    def send(self, to: str, text: str) -> str | None:
        method = "private-api" if self.private_api else "apple-script"
        body = {
            "chatGuid": bluebubbles_chat_guid(to),
            "message": text,
            "method": method,
            "tempGuid": f"bruno-{uuid.uuid4()}",
        }
        try:
            data = self._call("POST", "/api/v1/message/text", body)
        except BlueBubblesError as exc:
            # AppleScript sends to a brand-new 1:1 chat on their own (the server
            # falls back to the buddy). The Private API may need the chat to exist
            # first, so start it with /chat/new, unless the error suggests the
            # first attempt might have gone through (never double-text).
            if method != "private-api" or not exc.safe_to_retry:
                raise
            log.info("BlueBubbles private-api send to %s failed (%s); starting a new chat", to, exc)
            data = self._call("POST", "/api/v1/chat/new", {
                "addresses": [normalize_phone(to)], "message": text, "method": "private-api", "service": "iMessage",
                "tempGuid": f"bruno-{uuid.uuid4()}",
            })
            chat = data.get("data") if isinstance(data.get("data"), dict) else {}
            messages = [m for m in (chat.get("messages") or []) if isinstance(m, dict)]
            return messages[0].get("guid") if messages else None
        msg = data.get("data")
        return msg.get("guid") if isinstance(msg, dict) else None

    def typing(self, to: str) -> None:
        if not self.private_api:
            return  # typing bubbles need the BlueBubbles Private API
        guid = quote(bluebubbles_chat_guid(to), safe="")
        try:
            self._call("POST", f"/api/v1/chat/{guid}/typing", timeout=5)
        except MessagingError:
            pass  # cosmetic only

    def ping(self) -> str:
        """Check the server is reachable and the password is right."""
        data = self._call("GET", "/api/v1/ping", timeout=10)
        return str(data.get("data") or data.get("message") or "pong")

    def server_info(self) -> dict:
        """Server version, the iMessage account and Private API status (best effort)."""
        try:
            data = self._call("GET", "/api/v1/server/info", timeout=10).get("data")
        except MessagingError:
            return {}
        return data if isinstance(data, dict) else {}


class BlueBubblesError(MessagingError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"BlueBubbles error {status}: {detail}")
        self.status = status
        self.detail = detail

    @property
    def safe_to_retry(self) -> bool:
        """False when the message may have been sent anyway (send timeouts) or
        retrying can't help (bad password, bad request)."""
        if self.status in (400, 401, 403):
            return False
        detail = self.detail.lower()
        return not any(w in detail for w in ("database", "timed out", "timeout", "already queued"))


def bluebubbles_base_url(url: str) -> str:
    """Accept http://192.168.1.20:1234, https://x.trycloudflare.com/ or a URL
    that already ends in /api/v1."""
    url = (url or "").strip().rstrip("/")
    if url.endswith("/api/v1"):
        url = url[: -len("/api/v1")]
    if url and "://" not in url:
        url = "http://" + url
    return url


def bluebubbles_chat_guid(phone: str) -> str:
    """1:1 iMessage chat GUID, e.g. iMessage;-;+15551234567 (groups use ;+;)."""
    return f"iMessage;-;{normalize_phone(phone)}"


def _bluebubbles_error_text(data: dict) -> str:
    err = data.get("error")
    if isinstance(err, dict):
        # The server sends {"type", "message"}; the docs say {"type", "error"}.
        detail = err.get("message") or err.get("error") or err.get("type")
        if detail:
            return str(detail)
    elif err:
        return str(err)
    return str(data.get("message") or "")


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


def parse_bluebubbles_inbound(payload: dict) -> InboundText | None:
    """A BlueBubbles webhook event -> an InboundText from a customer, or None.

    The server POSTs ``{"type": "new-message", "data": <message>}`` where the
    message has ``guid``, ``text``, ``isFromMe``, ``handle.address``,
    ``chats[].guid`` and ``attachments``. Skipped: other event types, our own
    sends (they echo back with isFromMe), group chats (``;+;`` GUIDs),
    tapbacks/reactions, group events, and senders identified only by an Apple ID
    email (Bruno keys customers by phone number).
    """
    if not isinstance(payload, dict) or payload.get("type") != "new-message":
        return None
    msg = payload.get("data")
    if not isinstance(msg, dict) or msg.get("isFromMe"):
        return None
    if msg.get("associatedMessageGuid") or msg.get("associatedMessageType") not in (None, 0, "0", ""):
        return None  # tapback ("Loved 'see you at 6'") or other reaction
    if msg.get("itemType") not in (None, 0):
        return None  # rename / participant change / etc.
    chats = [c for c in (msg.get("chats") or []) if isinstance(c, dict)]
    chat_guid = str(chats[0].get("guid") or "") if chats else ""
    if ";+;" in chat_guid:
        return None
    handle = msg.get("handle") if isinstance(msg.get("handle"), dict) else {}
    address = str(handle.get("address") or "")
    if not address and ";-;" in chat_guid:
        address = chat_guid.split(";-;", 1)[1]
    if not address or "@" in address:
        if address:
            log.info("ignoring BlueBubbles message from an email handle")
        return None
    content = (msg.get("text") or "").replace("\ufffc", "").strip()  # U+FFFC marks inline attachments
    attachments = msg.get("attachments") or []
    if not (content or attachments):
        return None
    return InboundText(
        from_number=normalize_phone(address),
        content=content or "[sent an attachment]",
        message_id=msg.get("guid"),
    )


def build_messenger(settings: Settings) -> Messenger:
    provider = settings.messaging_provider
    if provider == "auto":
        if settings.bluebubbles_ready:
            provider = "bluebubbles"
        elif settings.sendblue_ready:
            provider = "sendblue"
        else:
            provider = "console"
    if provider == "bluebubbles":
        return BlueBubblesMessenger(settings)
    if provider == "sendblue":
        return SendblueMessenger(settings)
    if provider == "console":
        return ConsoleMessenger()
    raise ValueError(f"Unknown MESSAGING_PROVIDER: {settings.messaging_provider}")
