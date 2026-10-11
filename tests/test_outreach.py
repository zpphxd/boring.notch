import random
from dataclasses import replace
from datetime import timedelta

from bruno import learning, outreach
from bruno.agent import Bruno
from bruno.fsm import State
from bruno.llm import LLMError
from bruno.models import Conversation, Message, Order, Outreach
from tests.conftest import DAVE_PHONE, FRIDAY_430PM, make_customer, make_settings


class Writer:
    """LLM stub for free-form writing."""

    name = "writer"

    def __init__(self, *drafts):
        self.drafts = list(drafts)
        self.prompts = []

    def write(self, system, prompt):
        self.prompts.append((system, prompt))
        if not self.drafts:
            raise LLMError("out of drafts")
        return self.drafts.pop(0)

    def generate_turn(self, ctx):  # pragma: no cover
        raise LLMError("n/a")


def test_template_outreach_when_no_model(session, deps, settings):
    dave = make_customer(session, settings, now=FRIDAY_430PM)
    o = outreach.reach_out(session, deps, dave, FRIDAY_430PM, mode="auto")
    assert o.status == "sent" and o.author == "template"
    text = deps.messenger.sent[0][1]
    assert "Dave" in text and "Marco" in text and "Tony's" in text
    assert "large pepperoni pizza" in text and "6:00" in text
    assert text.endswith("(Reply STOP to opt out)")  # first contact only
    conv = session.query(Conversation).one()
    assert conv.state == State.AWAITING_RESPONSE and conv.outreach_id == o.id


def test_llm_written_outreach_and_guardrails(session, deps, settings):
    dave = make_customer(session, settings, now=FRIDAY_430PM)
    writer = Writer(
        "Dear valued customer, enjoy 20% off! Only $9.99 today: https://x.co",  # rejected
        "hey Dave, Marco from Tony's 🍕 been a few weeks, want your large pepperoni for 6? reply here or just call",
    )
    d = replace(deps, llm=writer)
    o = outreach.reach_out(session, d, dave, FRIDAY_430PM, mode="auto")
    assert o.author == "llm"
    assert o.message.startswith("hey Dave, Marco from Tony's")
    system, prompt = writer.prompts[1]
    assert "rejected" in prompt and "mentions a price" in prompt and "contains a link" in prompt
    assert "Their usual: large pepperoni pizza" in prompt
    assert "first time we've texted them" in system


def test_second_outreach_has_no_opt_out_footer_and_avoids_repeats(session, deps, settings):
    dave = make_customer(session, settings, now=FRIDAY_430PM)
    outreach.reach_out(session, deps, dave, FRIDAY_430PM, mode="auto")
    Bruno(session, deps, clock=lambda: FRIDAY_430PM).handle_inbound_text(DAVE_PHONE, "no thanks")
    writer = Writer("Hey Dave, it's Marco at Tony's. Pepperoni night? I can have it ready by 6, reply or call.")
    later = FRIDAY_430PM + timedelta(days=10)
    o = outreach.reach_out(session, replace(deps, llm=writer), dave, later, mode="auto")
    assert "STOP" not in o.message
    assert "don't repeat these" in writer.prompts[0][1]


def test_check_draft():
    assert outreach.check_draft("hey it's Marco from Tony's, usual tonight?", True, "Marco", "Tony's") == []
    problems = outreach.check_draft("Hey! want pizza?!!! 🍕🍕", True, "Marco", "Tony's")
    assert "more than one emoji" in problems and "too many exclamation marks" in problems
    assert any("first text must say" in p for p in problems)


def test_approve_mode_drafts_then_sends(session, deps, settings):
    dave = make_customer(session, settings, now=FRIDAY_430PM)
    o = outreach.reach_out(session, deps, dave, FRIDAY_430PM, mode="approve")
    assert o.status == "pending_approval" and deps.messenger.sent == []
    outreach.approve(session, deps, o.id, FRIDAY_430PM, edited_text="Dave! Marco here at Tony's. Usual tonight?")
    assert o.status == "sent" and o.author == "owner"
    assert deps.messenger.sent[0][1] == "Dave! Marco here at Tony's. Usual tonight?"


def test_autopilot_respects_timing_window_and_cap(session, deps, settings):
    d = replace(deps, settings=make_settings(autopilot_enabled=True, outreach_mode="auto", daily_outreach_cap=2))
    for k in range(4):
        make_customer(session, settings, now=FRIDAY_430PM, phone=f"+1555777{k:04d}", name=f"Pat{k}")
    # 3:30 PM local: typical 6 PM order -> best time 4:45 PM, so not yet
    early = FRIDAY_430PM - timedelta(hours=1)
    assert outreach.autopilot_tick(session, d, early)["sent"] == 0
    # 5:00 PM: due, but capped at 2 a day
    due = FRIDAY_430PM + timedelta(minutes=30)
    assert outreach.autopilot_tick(session, d, due)["sent"] == 2
    assert outreach.autopilot_tick(session, d, due + timedelta(minutes=5))["sent"] == 0
    # 11 PM: outside the send window
    assert outreach.autopilot_tick(session, d, FRIDAY_430PM + timedelta(hours=7))["sent"] == 0


def test_autopilot_off_does_nothing(session, deps, settings):
    make_customer(session, settings, now=FRIDAY_430PM)
    assert outreach.autopilot_tick(session, deps, FRIDAY_430PM + timedelta(minutes=30))["sent"] == 0


def test_bandit_shifts_toward_what_converts(session, deps, settings):
    c = make_customer(session, settings, now=FRIDAY_430PM)
    old = FRIDAY_430PM - timedelta(days=30)
    for i in range(30):
        tone = "brief" if i % 2 else "playful"
        o = Outreach(customer_id=c.id, strategy="usual", tone=tone, message="x", status="sent", sent_at=old, created_at=old)
        session.add(o)
        session.flush()
        if tone == "brief":
            order = Order(customer_id=c.id, items_json=[], total_amount=20, order_date=old, channel="text", status="completed")
            session.add(order)
            session.flush()
            o.converted_order_id = order.id
    picks = [learning.choose_approach(session, "drifting", settings, FRIDAY_430PM, random.Random(i))[1] for i in range(50)]
    assert picks.count("brief") > 40


def test_pos_order_after_outreach_counts_as_win(session, deps, settings):
    dave = make_customer(session, settings, now=FRIDAY_430PM)
    o = outreach.reach_out(session, deps, dave, FRIDAY_430PM, mode="auto")
    order = Order(customer_id=dave.id, items_json=[], total_amount=30, order_date=FRIDAY_430PM + timedelta(hours=20),
                  channel="pos", status="completed")
    session.add(order)
    session.flush()
    assert learning.attribute_order(session, order, settings) is o
    stats = learning.summary(session, settings, FRIDAY_430PM + timedelta(days=1))
    assert stats["conversions"] == 1 and stats["attributed_revenue"] == 30.0


def test_nudge_once_when_order_stalls(session, deps, settings):
    dave = make_customer(session, settings, now=FRIDAY_430PM)
    outreach.reach_out(session, deps, dave, FRIDAY_430PM, mode="auto")
    Bruno(session, deps, clock=lambda: FRIDAY_430PM).handle_inbound_text(DAVE_PHONE, "add a coke")
    session.commit()
    assert outreach.nudge_stalled(session, deps, FRIDAY_430PM + timedelta(minutes=10)) == 0  # too soon
    later = FRIDAY_430PM + timedelta(minutes=40)
    assert outreach.nudge_stalled(session, deps, later) == 1
    assert outreach.nudge_stalled(session, deps, later + timedelta(minutes=30)) == 0  # only once
    last = session.query(Message).order_by(Message.id.desc()).first()
    assert last.role == "agent" and "6:00" in last.content
