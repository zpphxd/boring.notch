from dataclasses import replace
from datetime import datetime, timezone

import pytest

from bruno import outreach
from bruno.agent import Bruno
from bruno.delivery import DeliveryError, DeliveryQuote, DeliveryResult
from bruno.fsm import Intent, State
from bruno.llm import AgentTurn, LLMError
from bruno.menu import CartItem
from bruno.models import Conversation, Order, Outreach
from bruno.orchestrator import voice_greeting
from bruno.pos import POSError
from tests.conftest import DAVE_PHONE, FRIDAY_430PM, make_customer, make_settings


@pytest.fixture
def dave(session, settings):
    return make_customer(session, settings, now=FRIDAY_430PM)


def bruno(session, deps):
    return Bruno(session, deps, clock=lambda: FRIDAY_430PM)


def start(session, deps, customer):
    o = outreach.reach_out(session, deps, customer, FRIDAY_430PM, mode="auto")
    assert o and o.status == "sent"
    return o


def test_full_text_order_loop(session, deps, dave):
    agent = bruno(session, deps)
    o = start(session, deps, dave)

    out = agent.handle_inbound_text(DAVE_PHONE, "Swap pepperoni for mushroom, add a Diet Coke")
    assert out.state == State.CONFIRMING_PAYMENT
    assert "large mushroom pizza and diet coke" in out.reply.lower()
    assert "$25.37" in out.reply and "Visa •4242" in out.reply
    assert deps.pos.orders == []  # nothing fires without an explicit yes

    out = agent.handle_inbound_text(DAVE_PHONE, "yes")
    assert out.state == State.COMPLETED
    assert "$25.37" in out.reply
    [ticket] = deps.pos.orders
    assert ticket.total_cents == 2537
    assert [li.item_id for li in ticket.line_items] == ["pizza", "diet_coke"]
    assert ticket.pickup_at.hour == 18

    order = session.get(Order, out.order_id)
    assert order.channel == "text" and order.status == "completed"
    assert dave.total_orders == 7
    # Learning loop: the outreach got the reply and the conversion credited
    assert o.replied_at is not None and o.converted_order_id == order.id
    assert float(o.revenue) == 25.37


def test_replies_go_out_as_separate_bubbles(session, deps, dave):
    agent = bruno(session, deps)
    start(session, deps, dave)
    before = len(deps.messenger.sent)
    agent.handle_inbound_text(DAVE_PHONE, "add a coke")
    bubbles = [t for _, t in deps.messenger.sent[before:]]
    assert bubbles[0] == "You got it!"
    assert len(bubbles) == 2 and "$" in bubbles[1]


def test_human_delays_and_typing(session, settings, dave):
    from bruno.llm.mock_llm import MockLLM
    from bruno.menu import Menu
    from bruno.messaging import ConsoleMessenger
    from bruno.payments import MockPaymentProvider
    from bruno.pos import ConsolePOS
    from bruno.deps import Deps

    class TypingMessenger(ConsoleMessenger):
        typed = 0

        def typing(self, to):
            TypingMessenger.typed += 1

    slept = []
    d = Deps(
        settings=make_settings(human_delays=True), menu=Menu.load(settings.menu_path, 0.08), llm=MockLLM(),
        messenger=TypingMessenger(echo=False), pos=ConsolePOS(echo=False), payments=MockPaymentProvider(),
        sleep=slept.append,
    )
    bruno(session, d).handle_inbound_text(DAVE_PHONE, "can i get my usual")
    assert TypingMessenger.typed >= 1 and all(0.9 < s < 8 for s in slept)


def test_yes_to_outreach_still_shows_priced_recap_first(session, deps, dave):
    agent = bruno(session, deps)
    start(session, deps, dave)
    out = agent.handle_inbound_text(DAVE_PHONE, "yes please")
    assert out.state == State.CONFIRMING_PAYMENT
    assert "$21.06" not in out.reply and "$" in out.reply
    assert deps.pos.orders == []
    assert agent.handle_inbound_text(DAVE_PHONE, "yep").state == State.COMPLETED


def test_pickup_time_change_requires_reconfirm(session, deps, dave):
    agent = bruno(session, deps)
    start(session, deps, dave)
    agent.handle_inbound_text(DAVE_PHONE, "yes")
    out = agent.handle_inbound_text(DAVE_PHONE, "actually can it be around 7:15")
    assert out.state == State.CONFIRMING_PAYMENT
    assert "7:15" in out.reply
    assert agent.handle_inbound_text(DAVE_PHONE, "yes").state == State.COMPLETED
    assert deps.pos.orders[0].pickup_at.strftime("%H:%M") == "19:15"


def test_decline(session, deps, dave):
    agent = bruno(session, deps)
    start(session, deps, dave)
    out = agent.handle_inbound_text(DAVE_PHONE, "no thanks, not tonight")
    assert out.state == State.DECLINED
    assert deps.pos.orders == []


def test_stop_opts_out_and_teaches_the_bandit(session, deps, dave):
    agent = bruno(session, deps)
    o = start(session, deps, dave)
    out = agent.handle_inbound_text(DAVE_PHONE, "STOP")
    assert out.state == State.OPTED_OUT
    assert dave.opted_out and o.opted_out
    assert outreach.reach_out(session, deps, dave, FRIDAY_430PM, mode="auto") is None
    agent.handle_inbound_text(DAVE_PHONE, "START")
    assert not dave.opted_out


def test_duplicate_webhook_delivery_is_ignored(session, deps, dave):
    agent = bruno(session, deps)
    start(session, deps, dave)
    agent.handle_inbound_text(DAVE_PHONE, "yes", provider_message_id="msg-1")
    session.commit()
    sent = len(deps.messenger.sent)
    out = agent.handle_inbound_text(DAVE_PHONE, "yes", provider_message_id="msg-1")
    assert out.reply is None
    assert len(deps.messenger.sent) == sent
    assert deps.pos.orders == []


def test_unknown_number_can_order_from_scratch(session, deps):
    agent = bruno(session, deps)
    out = agent.handle_inbound_text("+15559990000", "hey can i get a large pizza and add a sprite")
    assert out.state == State.CONFIRMING_PAYMENT
    assert "paid at pickup" in out.reply
    out = agent.handle_inbound_text("+15559990000", "yes")
    assert out.state == State.COMPLETED
    assert "pay when you grab it" in out.reply
    assert deps.pos.orders[0].customer.name == "Guest 0000"


def test_memory_is_learned_and_reused(session, deps, dave):
    agent = bruno(session, deps)
    start(session, deps, dave)
    agent.handle_inbound_text(DAVE_PHONE, "yes. i like it well done btw")
    assert dave.memory == ["i like it well done btw"]
    agent.handle_inbound_text(DAVE_PHONE, "yes")
    assert deps.pos.orders[0].customer_notes == ["i like it well done btw"]


def test_handoff_pauses_bot_and_pings_owner(session, deps, dave):
    d = replace(deps, settings=make_settings(owner_phone="+15550001234"))
    agent = bruno(session, d)
    start(session, d, dave)
    out = agent.handle_inbound_text(DAVE_PHONE, "my last order was cold, I want a refund")
    conv = session.get(Conversation, out.conversation_id)
    assert conv.human_takeover and conv.needs_attention
    assert any(to == "+15550001234" and "needs a person" in t for to, t in d.messenger.sent)
    sent = len(d.messenger.sent)
    out = agent.handle_inbound_text(DAVE_PHONE, "hello??")
    assert out.reply is None and len(d.messenger.sent) == sent  # Bruno stays quiet

    agent.staff_reply(conv, "So sorry Dave, refunding now and your next pie is on us.")
    assert d.messenger.sent[-1] == (DAVE_PHONE, "So sorry Dave, refunding now and your next pie is on us.")


class ScriptedLLM:
    name = "scripted"

    def __init__(self, *turns):
        self.turns = list(turns)
        self.contexts = []

    def generate_turn(self, ctx):
        self.contexts.append(ctx)
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn

    def write(self, system, prompt):
        raise LLMError("not scripted")


def test_llm_cannot_skip_confirmation(session, deps, dave):
    llm = ScriptedLLM(
        AgentTurn(intent=Intent.CONFIRM_ORDER, cart=[CartItem(item_id="supreme", size="large")], reply="Done!"),
    )
    d = replace(deps, llm=llm)
    start(session, d, dave)
    out = bruno(session, d).handle_inbound_text(DAVE_PHONE, "yes but make it a supreme")
    assert out.state == State.CONFIRMING_PAYMENT
    assert deps.pos.orders == []


def test_placeholders_get_exact_numbers(session, deps, dave):
    llm = ScriptedLLM(
        AgentTurn(intent=Intent.UPDATE_ORDER, cart=[CartItem(item_id="pizza", size="large", modifiers=["pepperoni"])],
                  reply="easy. {order} for {when}, {total} {payment}. send it?"),
    )
    d = replace(deps, llm=llm)
    start(session, d, dave)
    out = bruno(session, d).handle_inbound_text(DAVE_PHONE, "usual pls")
    assert out.reply == "easy. large pepperoni pizza for 6:00, $22.14 on your Visa •4242. send it?"


def test_invented_prices_are_never_sent(session, deps, dave):
    llm = ScriptedLLM(
        AgentTurn(intent=Intent.UPDATE_ORDER, cart=[CartItem(item_id="pizza", size="large", modifiers=["pepperoni"])],
                  reply="that's $15 total, want it?"),
    )
    d = replace(deps, llm=llm)
    start(session, d, dave)
    out = bruno(session, d).handle_inbound_text(DAVE_PHONE, "usual")
    assert "$15" not in out.reply and "$22.14" in out.reply


def test_invalid_cart_gets_one_corrective_retry(session, deps, dave):
    llm = ScriptedLLM(
        AgentTurn(intent=Intent.UPDATE_ORDER, cart=[CartItem(item_id="pizza")], reply="Sure!"),
        AgentTurn(intent=Intent.UPDATE_ORDER, cart=[CartItem(item_id="pizza", size="large")], reply="Large it is!"),
    )
    d = replace(deps, llm=llm)
    start(session, d, dave)
    out = bruno(session, d).handle_inbound_text(DAVE_PHONE, "just a cheese pizza")
    assert len(llm.contexts) == 2
    assert "What size" in llm.contexts[1].corrections[0]
    assert out.state == State.CONFIRMING_PAYMENT


def test_llm_outage_falls_back_politely(session, deps, dave):
    d = replace(deps, llm=ScriptedLLM(LLMError("timeout")))
    start(session, d, dave)
    out = bruno(session, d).handle_inbound_text(DAVE_PHONE, "yes")
    assert "Give us a call" in out.reply
    assert out.state == State.AWAITING_RESPONSE


def test_pos_failure_refunds_and_flags(session, deps, dave):
    class BrokenPOS:
        def inject(self, order):
            raise POSError("kitchen offline")

    d = replace(deps, pos=BrokenPOS())
    agent = bruno(session, d)
    start(session, d, dave)
    agent.handle_inbound_text(DAVE_PHONE, "yes")
    out = agent.handle_inbound_text(DAVE_PHONE, "yes")
    assert out.state == State.FAILED
    assert "weren't charged" in out.reply
    assert len(d.payments.refunds) == 1
    assert session.query(Order).filter_by(channel="text").count() == 0


# --------------------------------------------------------------------------- delivery
class FakeDrive:
    def __init__(self, fail_quote=False, fail_create=False):
        self.fail_quote, self.fail_create = fail_quote, fail_create
        self.created, self.cancelled = [], []

    def quote(self, **kw):
        if self.fail_quote:
            raise DeliveryError("address undeliverable")
        return DeliveryQuote(fee_cents=799, dropoff_eta=datetime(2026, 10, 9, 23, 15, tzinfo=timezone.utc), external_id=kw["external_id"])

    def create(self, **kw):
        if self.fail_create:
            raise DeliveryError("no dashers")
        self.created.append(kw)
        return DeliveryResult(delivery_id=kw["external_id"], tracking_url="https://track.doordash.com/x",
                              dropoff_eta=datetime(2026, 10, 9, 23, 20, tzinfo=timezone.utc))

    def cancel(self, delivery_id):
        self.cancelled.append(delivery_id)


def test_delivery_via_doordash_drive(session, deps, dave):
    drive = FakeDrive()
    d = replace(deps, delivery=drive)
    agent = bruno(session, d)
    start(session, d, dave)
    out = agent.handle_inbound_text(DAVE_PHONE, "can you deliver it?")
    assert out.state == State.NEGOTIATING_MODIFICATIONS
    assert "address" in out.reply.lower()

    out = agent.handle_inbound_text(DAVE_PHONE, "12 Main St, Brooklyn NY")
    assert out.state == State.CONFIRMING_PAYMENT
    # $22.14 food + $4.99 delivery fee, ETA from DoorDash quote (7:15 PM local)
    assert "$27.13" in out.reply and "12 Main St" in out.reply and "7:15" in out.reply

    out = agent.handle_inbound_text(DAVE_PHONE, "yes")
    assert out.state == State.COMPLETED
    assert "DoorDash driver" in out.reply and "https://track.doordash.com/x" in out.reply
    assert drive.created[0]["address"].startswith("12 Main St")
    ticket = deps.pos.orders[0]
    assert ticket.fulfillment == "delivery" and ticket.delivery_fee_cents == 499 and ticket.total_cents == 2713
    assert dave.address.startswith("12 Main St")


def test_undeliverable_address(session, deps, dave):
    d = replace(deps, delivery=FakeDrive(fail_quote=True))
    agent = bruno(session, d)
    start(session, d, dave)
    out = agent.handle_inbound_text(DAVE_PHONE, "deliver to 1 Nowhere Rd")
    assert out.state == State.NEGOTIATING_MODIFICATIONS
    assert "can't deliver" in out.reply


def test_dispatch_failure_refunds(session, deps, dave):
    d = replace(deps, delivery=FakeDrive(fail_create=True))
    agent = bruno(session, d)
    start(session, d, dave)
    agent.handle_inbound_text(DAVE_PHONE, "deliver to 12 Main St")
    out = agent.handle_inbound_text(DAVE_PHONE, "yes")
    assert out.state == State.FAILED and len(d.payments.refunds) == 1 and deps.pos.orders == []


def test_marketplace_link_when_no_drive(session, deps, dave):
    d = replace(deps, settings=make_settings(doordash_store_url="https://doordash.com/store/tonys"))
    agent = bruno(session, d)
    start(session, d, dave)
    out = agent.handle_inbound_text(DAVE_PHONE, "can you deliver it")
    assert "https://doordash.com/store/tonys" in out.reply
    conv = session.get(Conversation, out.conversation_id)
    assert conv.fulfillment == "pickup"


# --------------------------------------------------------------------------- voice
def test_voice_shares_text_state(session, deps, dave):
    agent = bruno(session, deps)
    start(session, deps, dave)
    assert agent.voice_context(DAVE_PHONE).state == State.AWAITING_RESPONSE

    agent.handle_inbound_text(DAVE_PHONE, "swap pepperoni for mushroom")
    ctx = agent.voice_context(DAVE_PHONE)
    assert voice_greeting(ctx, deps.settings) == (
        "Hey Dave! I've got your large mushroom pizza from our texts. Want me to put that in?"
    )
    res = agent.voice_update_cart(
        DAVE_PHONE,
        [{"item_id": "pizza", "size": "large", "quantity": 1, "modifiers": ["mushroom"]}, {"item_id": "coke", "quantity": 1}],
        None,
    )
    assert res["ok"] and res["total"] == "$25.37"
    assert agent.voice_place_order(DAVE_PHONE, customer_confirmed=False)["ok"] is False
    assert agent.voice_place_order(DAVE_PHONE, customer_confirmed=True)["ok"]
    assert deps.pos.orders[0].channel == "voice"
    assert session.query(Conversation).one().state == State.COMPLETED
    assert session.query(Outreach).one().converted_order_id is not None


def test_voice_place_order_requires_summary(session, deps, dave):
    res = bruno(session, deps).voice_place_order(DAVE_PHONE, customer_confirmed=True)
    assert res["ok"] is False
    assert deps.pos.orders == []


def test_addon_suggestion_from_company_history(session, deps, settings):
    # Lots of customers get garlic knots with their pizza
    for k in range(4):
        make_customer(session, settings, now=FRIDAY_430PM, phone=f"+1555444{k:04d}", name=f"C{k}", last_days_ago=2,
                      cart=[{"item_id": "pizza", "size": "large", "modifiers": [], "quantity": 1},
                            {"item_id": "garlic_knots", "quantity": 1}])
    dave = make_customer(session, settings, now=FRIDAY_430PM)
    llm = ScriptedLLM(AgentTurn(intent=Intent.QUESTION, cart=[CartItem(item_id="pizza", size="large", modifiers=["pepperoni"])], reply="hey!"))
    d = replace(deps, llm=llm)
    start(session, d, dave)
    bruno(session, d).handle_inbound_text(DAVE_PHONE, "hi who is this")
    assert "Garlic Knots" in llm.contexts[0].system_prompt


def test_upsell_never_rides_along_with_a_recap(session, deps, settings):
    for k in range(4):
        make_customer(session, settings, now=FRIDAY_430PM, phone=f"+1555445{k:04d}", name=f"K{k}", last_days_ago=2,
                      cart=[{"item_id": "pizza", "size": "large", "modifiers": [], "quantity": 1},
                            {"item_id": "sprite", "quantity": 1}])
    dave = make_customer(session, settings, now=FRIDAY_430PM)
    llm = ScriptedLLM(AgentTurn(
        intent=Intent.UPDATE_ORDER,
        cart=[CartItem(item_id="pizza", size="large", halves=[["mushroom", "onion"], ["pepperoni"]])],
        reply="nice. {order} ready around {when}, {total} {payment}. want me to put it in?\n\nwant a Sprite with that?"))
    d = replace(deps, llm=llm)
    start(session, d, dave)
    out = bruno(session, d).handle_inbound_text(DAVE_PHONE, "half veg half pepperoni")
    assert "Sprite" not in out.reply and "put it in" in out.reply
    assert "half mushroom & onion / half pepperoni" in out.reply
