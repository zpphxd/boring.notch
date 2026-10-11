from dataclasses import replace

import pytest

from bruno.agent import Bruno
from bruno.fsm import Intent, State
from bruno.llm import AgentTurn, LLMError
from bruno.menu import CartItem
from bruno.models import Conversation, Order
from bruno.pos import POSError
from tests.conftest import DAVE_PHONE, FRIDAY_430PM, make_customer


@pytest.fixture
def dave(session, settings):
    return make_customer(session, settings, now=FRIDAY_430PM)


def bruno(session, deps):
    return Bruno(session, deps, clock=lambda: FRIDAY_430PM)


def texts(deps):
    return [t for _, t in deps.messenger.sent]


def test_outreach_message_matches_playbook(session, deps, dave):
    conv = bruno(session, deps).start_outreach(dave)
    assert conv.state == State.AWAITING_RESPONSE
    msg = texts(deps)[-1]
    assert msg.startswith("Hey Dave! It's Marco from Tony's.")
    assert "your usual Friday large pepperoni pizza" in msg
    assert "pickup around 6:00" in msg
    assert "call me right back" not in msg and "give me a call right back at this number" in msg
    assert "Reply STOP to opt out." in msg
    assert conv.cart_json[0]["modifiers"] == ["pepperoni"]


def test_full_text_order_loop(session, deps, dave):
    agent = bruno(session, deps)
    agent.start_outreach(dave)

    out = agent.handle_inbound_text(DAVE_PHONE, "Swap pepperoni for mushroom, add a Diet Coke")
    assert out.state == State.CONFIRMING_PAYMENT
    assert "Large Pizza w/ Mushroom" in out.reply
    assert "Diet Coke" in out.reply
    assert "Total $25.37" in out.reply and "Visa •4242" in out.reply
    assert deps.pos.orders == []  # nothing fires without an explicit yes

    out = agent.handle_inbound_text(DAVE_PHONE, "yes")
    assert out.state == State.COMPLETED
    assert "Charged $25.37" in out.reply
    [ticket] = deps.pos.orders
    assert ticket.total_cents == 2537
    assert [li.item_id for li in ticket.line_items] == ["pizza", "diet_coke"]
    assert ticket.pickup_at.hour == 18

    order = session.get(Order, out.order_id)
    assert order.channel == "text" and order.status == "completed"
    assert dave.total_orders == 7
    assert not dave.cadence.drift_threshold_flag  # just ordered


def test_yes_to_outreach_still_shows_priced_summary_first(session, deps, dave):
    agent = bruno(session, deps)
    agent.start_outreach(dave)
    out = agent.handle_inbound_text(DAVE_PHONE, "yes please")
    assert out.state == State.CONFIRMING_PAYMENT
    assert "Reply YES to place it" in out.reply
    assert deps.pos.orders == []
    out = agent.handle_inbound_text(DAVE_PHONE, "yep")
    assert out.state == State.COMPLETED


def test_pickup_time_change_requires_reconfirm(session, deps, dave):
    agent = bruno(session, deps)
    agent.start_outreach(dave)
    agent.handle_inbound_text(DAVE_PHONE, "yes")
    out = agent.handle_inbound_text(DAVE_PHONE, "actually can it be around 7:15")
    assert out.state == State.CONFIRMING_PAYMENT
    assert "pickup ~7:15" in out.reply
    out = agent.handle_inbound_text(DAVE_PHONE, "yes")
    assert out.state == State.COMPLETED
    assert deps.pos.orders[0].pickup_at.strftime("%H:%M") == "19:15"


def test_decline(session, deps, dave):
    agent = bruno(session, deps)
    agent.start_outreach(dave)
    out = agent.handle_inbound_text(DAVE_PHONE, "no thanks, not tonight")
    assert out.state == State.DECLINED
    assert deps.pos.orders == []


def test_stop_opts_out(session, deps, dave):
    agent = bruno(session, deps)
    agent.start_outreach(dave)
    out = agent.handle_inbound_text(DAVE_PHONE, "STOP")
    assert out.state == State.OPTED_OUT
    assert dave.opted_out
    assert "unsubscribed" in out.reply
    assert agent.start_outreach(dave) is None
    agent.handle_inbound_text(DAVE_PHONE, "START")
    assert not dave.opted_out


def test_duplicate_webhook_delivery_is_ignored(session, deps, dave):
    agent = bruno(session, deps)
    agent.start_outreach(dave)
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
    assert "pay at pickup" in out.reply
    out = agent.handle_inbound_text("+15559990000", "yes")
    assert out.state == State.COMPLETED
    assert "pay when you pick up" in out.reply
    assert deps.pos.orders[0].customer.name == "Guest 0000"


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


def test_llm_cannot_skip_confirmation(session, deps, dave):
    # Model claims "confirm" and swaps the cart in the same turn: must re-confirm.
    llm = ScriptedLLM(
        AgentTurn(intent=Intent.CONFIRM_ORDER, cart=[CartItem(item_id="supreme", size="large")], reply="Done!"),
    )
    agent = bruno(session, replace(deps, llm=llm))
    agent.start_outreach(dave)
    out = agent.handle_inbound_text(DAVE_PHONE, "yes but make it a supreme")
    assert out.state == State.CONFIRMING_PAYMENT
    assert deps.pos.orders == []


def test_invalid_cart_gets_one_corrective_retry(session, deps, dave):
    llm = ScriptedLLM(
        AgentTurn(intent=Intent.UPDATE_ORDER, cart=[CartItem(item_id="pizza")], reply="Sure!"),
        AgentTurn(intent=Intent.UPDATE_ORDER, cart=[CartItem(item_id="pizza", size="large")], reply="Large it is!"),
    )
    agent = bruno(session, replace(deps, llm=llm))
    agent.start_outreach(dave)
    out = agent.handle_inbound_text(DAVE_PHONE, "just a cheese pizza")
    assert len(llm.contexts) == 2
    assert "What size" in llm.contexts[1].corrections[0]
    assert out.state == State.CONFIRMING_PAYMENT


def test_llm_outage_falls_back_politely(session, deps, dave):
    agent = bruno(session, replace(deps, llm=ScriptedLLM(LLMError("timeout"))))
    agent.start_outreach(dave)
    out = agent.handle_inbound_text(DAVE_PHONE, "yes")
    assert "trouble on my end" in out.reply
    assert out.state == State.AWAITING_RESPONSE


def test_pos_failure_refunds_and_flags(session, deps, dave):
    class BrokenPOS:
        def inject(self, order):
            raise POSError("kitchen offline")

    d = replace(deps, pos=BrokenPOS())
    agent = bruno(session, d)
    agent.start_outreach(dave)
    agent.handle_inbound_text(DAVE_PHONE, "yes")
    out = agent.handle_inbound_text(DAVE_PHONE, "yes")
    assert out.state == State.FAILED
    assert "weren't charged" in out.reply
    assert len(d.payments.refunds) == 1
    assert session.query(Order).filter_by(channel="text").count() == 0


def test_voice_shares_text_state(session, deps, dave):
    agent = bruno(session, deps)
    agent.start_outreach(dave)
    # Fresh outreach: voice greets with the usual
    assert agent.voice_context(DAVE_PHONE).state == State.AWAITING_RESPONSE

    agent.handle_inbound_text(DAVE_PHONE, "swap pepperoni for mushroom")
    ctx = agent.voice_context(DAVE_PHONE)
    from bruno.orchestrator import voice_greeting

    assert voice_greeting(ctx, deps.settings) == (
        "Hey Dave! I've got your large mushroom pizza from our texts. Want me to put that in?"
    )

    # Caller adds a coke by voice and confirms
    res = agent.voice_update_cart(
        DAVE_PHONE,
        [
            {"item_id": "pizza", "size": "large", "quantity": 1, "modifiers": ["mushroom"]},
            {"item_id": "coke", "quantity": 1},
        ],
        None,
    )
    assert res["ok"] and res["total"] == "$25.37"
    assert agent.voice_place_order(DAVE_PHONE, customer_confirmed=False)["ok"] is False
    done = agent.voice_place_order(DAVE_PHONE, customer_confirmed=True)
    assert done["ok"]
    assert deps.pos.orders[0].channel == "voice"
    conv = session.query(Conversation).one()
    assert conv.state == State.COMPLETED


def test_voice_place_order_requires_summary(session, deps, dave):
    agent = bruno(session, deps)
    res = agent.voice_place_order(DAVE_PHONE, customer_confirmed=True)
    assert res["ok"] is False
    assert deps.pos.orders == []
