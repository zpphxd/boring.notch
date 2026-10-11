"""Autonomous outreach: who to text, when, what to say, and gentle follow-up.

* Who: customers the learning layer flags as drifting / lapsed / one-time,
  ranked by value, minus anyone opted out, recently contacted or mid-chat.
* When: each person's own rhythm, ~75 min before they usually order,
  inside the restaurant's send window, capped per day.
* What: written fresh by the LLM for that person (their usual, how long it's
  been, what they've told us, weather), in a strategy + tone picked by the
  bandit in `learning.py`, then checked by guardrails. Falls back to varied
  templates if the model is unavailable or the draft fails the checks.
"""

import logging
import random
import re
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bruno import cadence, humanize, learning
from bruno.agent import Bruno
from bruno.deps import Deps
from bruno.fsm import State
from bruno.llm import LLMError
from bruno.locks import customer_lock
from bruno.models import Conversation, Customer, Message, Outreach
from bruno.orchestrator import WEEKDAYS, gap_phrase, propose_pickup
from bruno.timeutil import display_time, earliest_pickup, parse_hhmm, to_local
from bruno.weather import current_weather

log = logging.getLogger(__name__)

LEAD_MINUTES = 75
MAX_CHARS = 320
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
TONES = {
    "casual": "relaxed and friendly, like texting a regular you know",
    "playful": "light and a little funny, warm, never cheesy",
    "brief": "very short and direct but still friendly",
}


def _goal(strategy: str, usual: str, when: str) -> str:
    return {
        "usual": f"offer to put in their usual ({usual}) for pickup around {when}",
        "win_back": f"it's been a while: warmly say you've missed them and offer their usual ({usual}) for around {when}",
        "second_order": f"they've ordered once ({usual}): hope they liked it and offer to make it again for around {when}",
    }.get(strategy, f"offer their usual ({usual}) for around {when}")


def is_first_contact(session: Session, customer: Customer) -> bool:
    sent = session.scalar(
        select(func.count(Outreach.id)).where(Outreach.customer_id == customer.id, Outreach.status == "sent")
    )
    talked = session.scalar(select(func.count(Conversation.id)).where(Conversation.customer_id == customer.id))
    return not sent and not talked


def check_draft(text: str, first_contact: bool, agent: str, restaurant: str) -> list[str]:
    problems = []
    if not text:
        problems.append("empty")
    if len(text) > MAX_CHARS:
        problems.append(f"too long ({len(text)} chars, max {MAX_CHARS})")
    if humanize.PRICE_RE.search(text):
        problems.append("mentions a price")
    if re.search(r"https?://|www\.", text):
        problems.append("contains a link")
    if humanize.BOT_TELLS.search(text):
        problems.append("sounds like marketing or a bot")
    if "{" in text or "}" in text:
        problems.append("unfilled placeholder")
    if len(EMOJI_RE.findall(text)) > 1:
        problems.append("more than one emoji")
    if text.count("!") > 2:
        problems.append("too many exclamation marks")
    if first_contact and agent.lower() not in text.lower() and restaurant.lower() not in text.lower():
        problems.append(f"first text must say it's {agent} from {restaurant}")
    return problems


def compose(
    session: Session, deps: Deps, customer: Customer, strategy: str, tone: str, now: datetime,
    rng: random.Random | None = None,
) -> tuple[str, str, str]:
    """Returns (message, author, proposed_pickup)."""
    rng = rng or deps.rng
    s = deps.settings
    t = customer.cadence
    pickup = propose_pickup(t, now, s)
    when = display_time(pickup)
    usual = deps.menu.describe(t.usual_order_json or []) or "the usual"
    days = (now - t.last_order_date).days if t and t.last_order_date else 30
    local = to_local(now, s.restaurant_timezone)
    first_contact = is_first_contact(session, customer)
    call_target = s.voice_phone_number or "this number"
    weather = current_weather(s)

    message, author = None, "llm"
    previous = [
        m for (m,) in session.execute(
            select(Outreach.message).where(Outreach.customer_id == customer.id, Outreach.status == "sent")
            .order_by(Outreach.id.desc()).limit(2)
        )
    ]
    system = f"""You write one text message from {s.agent_name} at {s.restaurant_name}, a neighborhood pizzeria, to one of its customers. It must read like a real person who knows them, texting from their phone. Not marketing.
Rules:
- 1-2 short sentences, under 260 characters.
- {"This is the first time we've texted them, so make it clear it's " + s.agent_name + " from " + s.restaurant_name + "." if first_contact else "Say who it is casually (e.g. '" + s.agent_name + " from " + s.restaurant_name + "')."}
- Tone: {TONES.get(tone, TONES['casual'])}.
- Goal: {_goal(strategy, usual, when)}.
- End with an easy question they can answer with "yes".
- Briefly mention they can just reply here or call {call_target}.
- No prices, discounts, promotions, links or hashtags. Don't make claims you can't back up (no "fresh batch just came out", no "we're slow tonight"). At most one emoji. No "Dear", no sign-off.
- Don't mention tracking, data or "our records"; remembering their usual is fine.
Output only the message text."""
    facts = [
        f"Customer first name: {customer.first_name}",
        f"Their usual: {usual}",
        f"Days since last order: {days}" + (f" (normally every ~{round(t.avg_days_between_orders)} days)" if t and t.avg_days_between_orders else ""),
        f"Today: {local.strftime('%A %I:%M %p')}",
        f"Proposed pickup time: {when}",
    ]
    if t and t.preferred_weekday is not None:
        facts.append(f"They usually order on {WEEKDAYS[t.preferred_weekday]}s")
    if customer.memory:
        facts.append("Things they've told us: " + "; ".join(customer.memory[-5:]))
    if weather:
        facts.append(f"Weather right now: {weather.summary}")
    if previous:
        facts.append("Texts we sent them before (don't repeat these): " + " | ".join(previous))
    prompt = "\n".join(facts)

    for attempt in range(2):
        try:
            draft = humanize.clean(deps.llm.write(system, prompt))
        except LLMError as exc:
            log.info("outreach writer unavailable (%s); using template", exc)
            break
        problems = check_draft(draft, first_contact, s.agent_name, s.restaurant_name)
        if not problems:
            message = draft
            break
        prompt += f"\n\nYour last draft was rejected ({'; '.join(problems)}). Rewrite it."

    if message is None:
        author = "template"
        day_line = ""
        if t and t.preferred_weekday == local.weekday():
            day_line = f"Happy {WEEKDAYS[local.weekday()]}! "
        template = rng.choice(humanize.OUTREACH_TEMPLATES.get(strategy, humanize.OUTREACH_TEMPLATES["usual"]))
        message = template.format(
            first=customer.first_name, agent=s.agent_name, restaurant=s.restaurant_name, gap=gap_phrase(days),
            usual=usual, when=when, day_line=day_line,
            call=f"call me at {s.voice_phone_number}" if s.voice_phone_number else "call me right back at this number",
        )
        message = re.sub(r"\. ([a-z])", lambda m: ". " + m.group(1).upper(), message)

    if first_contact:
        message += "\n(Reply STOP to opt out)"
    return message, author, pickup


# --------------------------------------------------------------------------- send / approve
def reach_out(
    session: Session, deps: Deps, customer: Customer, now: datetime, *, mode: str | None = None
) -> Outreach | None:
    """Draft (approve mode) or send (auto mode) a proactive text to one customer."""
    if customer.opted_out:
        return None
    tracker = customer.cadence or cadence.recompute_customer(session, customer, deps.settings, now)
    if not tracker.usual_order_json:
        return None
    mode = mode or deps.settings.outreach_mode
    strategy, tone = learning.choose_approach(session, tracker.segment, deps.settings, now, deps.rng)
    text, author, pickup = compose(session, deps, customer, strategy, tone, now)
    outreach = Outreach(
        customer_id=customer.id, strategy=strategy, tone=tone, message=text, author=author,
        proposed_pickup=pickup, status="pending_approval", created_at=now,
    )
    session.add(outreach)
    session.flush()
    if mode == "approve":
        return outreach
    Bruno(session, deps, clock=lambda: now).open_outreach_conversation(customer, outreach, pickup)
    return outreach


def approve(session: Session, deps: Deps, outreach_id: int, now: datetime, edited_text: str | None = None) -> Outreach | None:
    outreach = session.get(Outreach, outreach_id)
    if not outreach or outreach.status != "pending_approval":
        return None
    customer = session.get(Customer, outreach.customer_id)
    if customer.opted_out or cadence.open_conversation(session, customer.id, deps.settings, now):
        outreach.status = "skipped"
        return outreach
    pickup = outreach.proposed_pickup or propose_pickup(customer.cadence, now, deps.settings)
    if edited_text and edited_text.strip() != outreach.message:
        outreach.message = edited_text.strip()
        outreach.author = "owner"
    elif parse_hhmm(pickup) and earliest_pickup(now, deps.settings.restaurant_timezone, deps.settings.min_prep_minutes).strftime("%H:%M") > pickup:
        # The time we offered has passed since this was drafted: rewrite it.
        outreach.message, outreach.author, pickup = compose(
            session, deps, customer, outreach.strategy, outreach.tone, now
        )
    outreach.proposed_pickup = pickup
    Bruno(session, deps, clock=lambda: now).open_outreach_conversation(customer, outreach, pickup)
    return outreach


def skip(session: Session, outreach_id: int) -> None:
    outreach = session.get(Outreach, outreach_id)
    if outreach and outreach.status == "pending_approval":
        outreach.status = "skipped"


# --------------------------------------------------------------------------- autopilot
def _local_today_at(local_now: datetime, hhmm: str) -> datetime:
    h, m = parse_hhmm(hhmm) or (0, 0)
    return local_now.replace(hour=h, minute=m, second=0, microsecond=0)


def ideal_send_time(customer: Customer, deps: Deps, local_now: datetime) -> datetime:
    """~75 min before they usually order, clamped to the send window."""
    s = deps.settings
    typical = (customer.cadence.typical_pickup_time if customer.cadence else None) or "18:00"
    ideal = _local_today_at(local_now, typical) - timedelta(minutes=LEAD_MINUTES)
    start, end = _local_today_at(local_now, s.send_window_start), _local_today_at(local_now, s.send_window_end)
    return min(max(ideal, start), end)


def autopilot_tick(session: Session, deps: Deps, now: datetime) -> dict:
    s = deps.settings
    result = {"drafted": 0, "sent": 0, "expired": 0}
    local = to_local(now, s.restaurant_timezone)
    day_start_utc = now - timedelta(hours=local.hour, minutes=local.minute, seconds=local.second)

    # Drafts from previous days are stale.
    for o in session.scalars(
        select(Outreach).where(Outreach.status == "pending_approval", Outreach.created_at < day_start_utc)
    ):
        o.status = "skipped"
        result["expired"] += 1

    if not s.autopilot_enabled:
        return result
    if not (_local_today_at(local, s.send_window_start) <= local <= _local_today_at(local, s.send_window_end)):
        return result

    today = session.scalar(
        select(func.count(Outreach.id)).where(
            Outreach.created_at >= day_start_utc, Outreach.status.in_(("sent", "pending_approval"))
        )
    ) or 0
    budget = s.daily_outreach_cap - today
    if budget <= 0:
        return result
    touched_today = {
        cid for (cid,) in session.execute(
            select(Outreach.customer_id).where(
                (Outreach.created_at >= day_start_utc) | (Outreach.status == "pending_approval")
            )
        )
    }
    for customer in cadence.outreach_candidates(session, s, now):
        if budget <= 0:
            break
        if customer.id in touched_today or not customer.phone_number:
            continue
        if local < ideal_send_time(customer, deps, local):
            continue
        with customer_lock(customer.phone_number):
            outreach = reach_out(session, deps, customer, now)
        if outreach is None:
            continue
        budget -= 1
        result["drafted" if outreach.status == "pending_approval" else "sent"] += 1
    return result


def nudge_stalled(session: Session, deps: Deps, now: datetime) -> int:
    """One gentle follow-up when someone was mid-order and went quiet."""
    s = deps.settings
    local = to_local(now, s.restaurant_timezone)
    closing = _local_today_at(local, s.closing_time)
    if local > closing - timedelta(minutes=45):
        return 0
    cutoff = now - timedelta(minutes=s.nudge_after_minutes)
    convs = session.scalars(
        select(Conversation).where(
            Conversation.state.in_((State.NEGOTIATING_MODIFICATIONS.value, State.CONFIRMING_PAYMENT.value)),
            Conversation.human_takeover.is_(False),
            Conversation.nudged_at.is_(None),
            Conversation.updated_at < cutoff,
            Conversation.updated_at > now - timedelta(hours=3),
        )
    ).all()
    sent = 0
    for conv in convs:
        last = session.scalars(
            select(Message).where(Message.conversation_id == conv.id, Message.role.in_(("agent", "customer", "staff")))
            .order_by(Message.id.desc()).limit(1)
        ).first()
        if not last or last.role != "agent" or not conv.cart_json:
            continue
        customer = session.get(Customer, conv.customer_id)
        if customer.opted_out:
            continue
        text = deps.rng.choice(humanize.NUDGE_TEMPLATES).format(when=display_time(conv.pickup_time or ""))
        with customer_lock(customer.phone_number):
            session.refresh(conv)
            if conv.nudged_at or conv.updated_at >= cutoff:
                continue  # they replied while we were looking
            agent = Bruno(session, deps, clock=lambda: now)
            agent.send(customer.phone_number, text)
            agent._record(conv, "agent", "text", text)
            conv.nudged_at = now
        sent += 1
    return sent
