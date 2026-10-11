import json
from dataclasses import replace
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from bruno.api import create_app
from bruno.dashboard import bluebubbles_check
from bruno.messaging import (
    BlueBubblesMessenger,
    ConsoleMessenger,
    MessagingError,
    SendblueMessenger,
    build_messenger,
    parse_bluebubbles_inbound,
)
from bruno.models import Message
from tests.conftest import DAVE_PHONE, make_customer, make_settings

BB_URL = "https://bb.example.com"


class FakeBlueBubbles:
    """Records requests and answers like a BlueBubbles Server would."""

    def __init__(self, handler=None):
        self.requests: list[httpx.Request] = []
        self.handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.handler:
            resp = self.handler(request)
            if resp is not None:
                return resp
        path = request.url.path
        if path == "/api/v1/ping":
            return httpx.Response(200, json={"status": 200, "message": "Ping received!", "data": "pong"})
        if path == "/api/v1/server/info":
            return httpx.Response(200, json={"status": 200, "message": "ok", "data": {
                "server_version": "1.9.9", "detected_imessage": "tonys@icloud.com",
                "private_api": True, "helper_connected": True,
            }})
        if path == "/api/v1/message/text":
            body = json.loads(request.content)
            return httpx.Response(200, json={"status": 200, "message": "Message sent!", "data": {
                "guid": f"sent-{len(self.requests)}", "text": body["message"], "isFromMe": True,
                "tempGuid": body["tempGuid"],
            }})
        if path.endswith("/typing"):
            return httpx.Response(200, json={"status": 200, "message": "Successfully started typing!"})
        return httpx.Response(404, json={"status": 404, "message": "Not found"})

    def bodies(self, path: str) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path == path]


def messenger(fake: FakeBlueBubbles, **overrides) -> BlueBubblesMessenger:
    s = make_settings(bluebubbles_url=BB_URL, bluebubbles_password="hunter2", **overrides)
    return BlueBubblesMessenger(s, client=httpx.Client(transport=httpx.MockTransport(fake)))


def new_message(text="hi", guid="bb-1", address=DAVE_PHONE, chat_guid=None, **extra) -> dict:
    data = {
        "originalROWID": 42, "guid": guid, "text": text, "handleId": 7,
        "handle": {"originalROWID": 7, "address": address, "service": "iMessage"},
        "attachments": [], "isFromMe": False, "itemType": 0,
        "associatedMessageGuid": None, "associatedMessageType": "0",
        "chats": [{"originalROWID": 3, "guid": chat_guid or f"iMessage;-;{address}", "style": 45,
                   "chatIdentifier": address, "isArchived": False, "displayName": ""}],
        "dateCreated": 1760040000000,
    }
    data.update(extra)
    return {"type": "new-message", "data": data}


# --------------------------------------------------------------------------- sending
def test_send_request_shape():
    fake = FakeBlueBubbles()
    msg_id = messenger(fake).send("(555) 123-0001", "Hey Dave!")
    (req,) = fake.requests
    assert req.method == "POST"
    assert req.url.path == "/api/v1/message/text"
    assert req.url.params["password"] == "hunter2"
    body = json.loads(req.content)
    assert body["chatGuid"] == "iMessage;-;+15551230001"
    assert body["message"] == "Hey Dave!"
    assert body["method"] == "apple-script"
    assert body["tempGuid"]
    assert msg_id == "sent-1"


def test_send_uses_unique_temp_guids_and_private_api_method():
    fake = FakeBlueBubbles()
    bb = messenger(fake, bluebubbles_private_api=True)
    bb.send(DAVE_PHONE, "one")
    bb.send(DAVE_PHONE, "two")
    bodies = fake.bodies("/api/v1/message/text")
    assert {b["method"] for b in bodies} == {"private-api"}
    assert bodies[0]["tempGuid"] != bodies[1]["tempGuid"]


def test_base_url_is_normalized():
    fake = FakeBlueBubbles()
    s = make_settings(bluebubbles_url="192.168.1.20:1234/api/v1/", bluebubbles_password="pw")
    bb = BlueBubblesMessenger(s, client=httpx.Client(transport=httpx.MockTransport(fake)))
    bb.send(DAVE_PHONE, "hi")
    assert str(fake.requests[0].url).startswith("http://192.168.1.20:1234/api/v1/message/text?")


def test_send_failure_raises_messaging_error_with_server_detail():
    fake = FakeBlueBubbles(lambda r: httpx.Response(401, json={
        "status": 401, "message": "You are not authorized to access this resource",
        "error": {"type": "Authentication Error", "message": "You are not authorized to access this resource"},
    }))
    with pytest.raises(MessagingError, match="not authorized"):
        messenger(fake).send(DAVE_PHONE, "hi")
    assert len(fake.requests) == 1  # no retry on auth errors


def test_unreachable_server_raises_messaging_error():
    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)

    s = make_settings(bluebubbles_url=BB_URL, bluebubbles_password="pw")
    bb = BlueBubblesMessenger(s, client=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(MessagingError, match="unreachable"):
        bb.send(DAVE_PHONE, "hi")


def test_private_api_send_to_new_chat_falls_back_to_chat_new():
    def handler(request):
        if request.url.path == "/api/v1/message/text":
            return httpx.Response(500, json={"status": 500, "message": "iMessage error",
                                             "error": {"type": "iMessage Error", "message": "Chat does not exist"}})
        if request.url.path == "/api/v1/chat/new":
            return httpx.Response(200, json={"status": 200, "message": "Successfully created chat!",
                                             "data": {"guid": "iMessage;-;+15551230001", "messages": [{"guid": "new-1"}]}})
        return None

    fake = FakeBlueBubbles(handler)
    assert messenger(fake, bluebubbles_private_api=True).send(DAVE_PHONE, "hi Dave") == "new-1"
    (body,) = fake.bodies("/api/v1/chat/new")
    assert body["addresses"] == [DAVE_PHONE] and body["message"] == "hi Dave" and body["method"] == "private-api"


def test_send_timeout_is_not_retried():
    """A send that timed out may still have gone out: never double-text."""
    fake = FakeBlueBubbles(lambda r: httpx.Response(500, json={
        "status": 500, "message": "iMessage error",
        "error": {"type": "iMessage Error", "message": "Failed to send message! Message not found in database after 60 seconds!"},
    }))
    with pytest.raises(MessagingError):
        messenger(fake, bluebubbles_private_api=True).send(DAVE_PHONE, "hi")
    assert fake.bodies("/api/v1/chat/new") == []


def test_typing_only_with_private_api():
    fake = FakeBlueBubbles()
    messenger(fake).typing(DAVE_PHONE)
    assert fake.requests == []

    messenger(fake, bluebubbles_private_api=True).typing(DAVE_PHONE)
    (req,) = fake.requests
    assert req.method == "POST"
    assert unquote(req.url.raw_path.decode().split("?")[0]) == "/api/v1/chat/iMessage;-;+15551230001/typing"
    assert req.url.params["password"] == "hunter2"


def test_typing_never_raises():
    fake = FakeBlueBubbles(lambda r: httpx.Response(500, json={"status": 500, "message": "helper not connected"}))
    messenger(fake, bluebubbles_private_api=True).typing(DAVE_PHONE)

    def boom(request):
        raise httpx.ConnectTimeout("slow", request=request)

    s = make_settings(bluebubbles_url=BB_URL, bluebubbles_password="pw", bluebubbles_private_api=True)
    BlueBubblesMessenger(s, client=httpx.Client(transport=httpx.MockTransport(boom))).typing(DAVE_PHONE)


def test_ping_and_dashboard_check():
    fake = FakeBlueBubbles()
    bb = messenger(fake, bluebubbles_private_api=True)
    assert bb.ping() == "pong"
    msg, kind = bluebubbles_check(bb)
    assert kind == "ok" and "v1.9.9" in msg and "tonys@icloud.com" in msg and "helper connected" in msg


def test_provider_selection():
    assert isinstance(build_messenger(make_settings(messaging_provider="auto")), ConsoleMessenger)
    both = dict(bluebubbles_url=BB_URL, bluebubbles_password="pw", sendblue_api_key_id="k", sendblue_api_secret_key="s")
    assert isinstance(build_messenger(make_settings(messaging_provider="auto", **both)), BlueBubblesMessenger)
    assert isinstance(build_messenger(make_settings(messaging_provider="sendblue", **both)), SendblueMessenger)
    assert isinstance(
        build_messenger(make_settings(messaging_provider="auto", sendblue_api_key_id="k", sendblue_api_secret_key="s")),
        SendblueMessenger,
    )
    assert make_settings(bluebubbles_url=BB_URL, bluebubbles_password="pw").bluebubbles_ready
    assert not make_settings(bluebubbles_url=BB_URL).bluebubbles_ready


# --------------------------------------------------------------------------- inbound parsing
def test_parse_normal_message():
    inbound = parse_bluebubbles_inbound(new_message("  can I get my usual  ", address="5551230001"))
    assert inbound.from_number == DAVE_PHONE
    assert inbound.content == "can I get my usual"
    assert inbound.message_id == "bb-1"


def test_parse_ignores_own_messages_groups_reactions_and_other_events():
    assert parse_bluebubbles_inbound(new_message(isFromMe=True)) is None
    assert parse_bluebubbles_inbound(new_message(chat_guid="iMessage;+;chat123456")) is None
    assert parse_bluebubbles_inbound(new_message(
        'Loved "see you at 6"', associatedMessageGuid="p:0/bb-0", associatedMessageType="love")) is None
    assert parse_bluebubbles_inbound(new_message(address="dave@icloud.com")) is None
    assert parse_bluebubbles_inbound({**new_message(), "type": "updated-message"}) is None
    assert parse_bluebubbles_inbound({"type": "new-message", "data": None}) is None
    assert parse_bluebubbles_inbound(new_message("")) is None


def test_parse_attachment_only_message():
    payload = new_message("￼", attachments=[{"guid": "att-1", "mimeType": "image/jpeg", "transferName": "IMG_1.jpg"}])
    inbound = parse_bluebubbles_inbound(payload)
    assert inbound.content == "[sent an attachment]"


def test_parse_falls_back_to_chat_guid_address():
    payload = new_message(handle=None, chat_guid="SMS;-;+15551230001")
    assert parse_bluebubbles_inbound(payload).from_number == DAVE_PHONE


# --------------------------------------------------------------------------- end to end
@pytest.fixture
def bb_fake():
    return FakeBlueBubbles()


@pytest.fixture
def bb_client(deps, session_factory, bb_fake):
    d = replace(deps, messenger=messenger(bb_fake))
    return TestClient(create_app(d, session_factory))


def test_webhook_end_to_end_replies_through_bluebubbles(bb_client, bb_fake, session, settings, deps):
    make_customer(session, settings)
    r = bb_client.post("/webhook/bluebubbles", json=new_message("can I get my usual", guid="in-1"))
    assert r.json() == {"status": "accepted"}
    sent = bb_fake.bodies("/api/v1/message/text")
    assert sent and all(b["chatGuid"] == f"iMessage;-;{DAVE_PHONE}" for b in sent)
    assert "$22.14" in " ".join(b["message"] for b in sent)

    # BlueBubbles echoes our own sends back as new-message events: ignored.
    assert bb_client.post("/webhook/bluebubbles", json=new_message("echo", guid="sent-1", isFromMe=True)).json() == {
        "status": "ignored"}
    # Retried webhook with the same guid is processed once.
    before = len(bb_fake.bodies("/api/v1/message/text"))
    bb_client.post("/webhook/bluebubbles", json=new_message("can I get my usual", guid="in-1"))
    assert len(bb_fake.bodies("/api/v1/message/text")) == before
    session.expire_all()
    assert session.query(Message).filter_by(provider_message_id="in-1").count() == 1

    bb_client.post("/webhook/bluebubbles", json=new_message("yes", guid="in-2"))
    assert "Dave" in bb_fake.bodies("/api/v1/message/text")[-1]["message"]
    assert len(deps.pos.orders) == 1


def test_bluebubbles_webhook_requires_secret(deps, session_factory):
    d = replace(deps, settings=make_settings(webhook_secret="s3cret"))
    c = TestClient(create_app(d, session_factory))
    assert c.post("/webhook/bluebubbles", json=new_message()).status_code == 401
    assert c.post("/webhook/bluebubbles?secret=s3cret", json=new_message(isFromMe=True)).status_code == 200


def test_settings_page_lists_bluebubbles(bb_client, bb_fake):
    bb_client.post("/admin/login", data={"password": "pizza-pass-1"})
    page = bb_client.get("/admin/settings").text
    assert "/webhook/bluebubbles" in page and "Texting (BlueBubbles)" in page
    assert page.index("Texting (BlueBubbles)") < page.index("Texting (Sendblue)")
    assert "Grubhub" not in page

    r = bb_client.post("/admin/settings/test/bluebubbles")
    assert r.status_code == 200 and "answered" in r.text
    assert any(req.url.path == "/api/v1/ping" for req in bb_fake.requests)


def test_send_me_a_test_text_goes_through_bluebubbles(deps, session_factory, bb_fake):
    d = replace(deps, settings=make_settings(owner_phone="555-123-9999"), messenger=messenger(bb_fake))
    c = TestClient(create_app(d, session_factory))
    c.post("/admin/login", data={"password": "pizza-pass-1"})
    r = c.post("/admin/settings/test/text")
    assert "Test text sent" in r.text
    (body,) = bb_fake.bodies("/api/v1/message/text")
    assert body["chatGuid"] == "iMessage;-;+15551239999" and "Texting works" in body["message"]
