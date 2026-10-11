"""Making Bruno read like a person: placeholder filling (so numbers are always
right but the wording is natural), message bubbles, typing pauses and varied
fallback copy."""

import random
import re
import time
from collections.abc import Callable

PLACEHOLDER_RE = re.compile(r"\{(order|total|when|payment|address|eta)\}")
PRICE_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{1,2})?")
BOT_TELLS = re.compile(
    r"(as an ai|language model|i'?d be (happy|glad) to assist|valued customer|dear customer|"
    r"thank you for your (patience|inquiry)|please be advised|act now|limited time)",
    re.I,
)


def fill(template: str, facts: dict[str, str]) -> str:
    return PLACEHOLDER_RE.sub(lambda m: facts.get(m.group(1), ""), template)


def has_unverified_price(text: str, allowed: set[str]) -> bool:
    """True if text mentions a dollar amount we didn't compute ourselves."""
    return any(p.replace(" ", "") not in allowed for p in PRICE_RE.findall(text))


def clean(text: str) -> str:
    text = text.strip().strip('"').strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def split_bubbles(text: str, max_bubbles: int = 3) -> list[str]:
    """Blank lines separate iMessage bubbles, like a person sending 2 quick texts."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(parts) > max_bubbles:
        parts = parts[: max_bubbles - 1] + ["\n".join(parts[max_bubbles - 1 :])]
    return parts or [text.strip()]


def typing_seconds(text: str, rng: random.Random | None = None) -> float:
    rng = rng or random
    # ~6 chars/sec thumb typing, but nobody wants to wait 20s for a recap.
    return max(1.2, min(len(text) / 6.0, 6.5)) * rng.uniform(0.8, 1.2)


def deliver(
    send: Callable[[str], str | None],
    text: str,
    *,
    typing: Callable[[], None] | None = None,
    human_delays: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str | None]:
    """Send `text` as 1-3 bubbles with a typing indicator and natural pauses."""
    ids = []
    for i, bubble in enumerate(split_bubbles(text)):
        if human_delays:
            if typing:
                try:
                    typing()
                except Exception:
                    pass
            sleep(typing_seconds(bubble) if i else min(typing_seconds(bubble), 3.0))
        ids.append(send(bubble))
    return ids


# --------------------------------------------------------------------------- confirmations
# {payment} is a whole clause: "on your Visa •4242" / "paid at pickup" / "paid by a secure link I'll text you"
CONFIRM_PICKUP = [
    "So that's {order}, ready around {when}. Comes to {total} {payment}. Want me to put it in?",
    "Cool, {order} for {when}. {total} {payment}. Should I send it to the kitchen?",
    "Got you: {order}, ready ~{when}. {total} total, {payment}. Good to go?",
    "{order}, pickup around {when}, {total} {payment}. Want me to fire it?",
]
CONFIRM_DELIVERY = [
    "So that's {order}, dropped off at {address} around {when}. {total} with delivery, {payment}. Want me to put it in?",
    "Got it: {order} delivered to {address} (~{when}). Comes to {total} incl. delivery, {payment}. Good to go?",
]
READY_NUDGE = [
    "Just say the word and I'll put it in.",
    "Whenever you're ready, just say yes.",
    "Say yes and it's in.",
]


def capitalize_first(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def confirmation(facts: dict[str, str], delivery: bool, rng: random.Random | None = None) -> str:
    rng = rng or random
    return capitalize_first(fill(rng.choice(CONFIRM_DELIVERY if delivery else CONFIRM_PICKUP), facts))


# --------------------------------------------------------------------------- outreach fallbacks
OUTREACH_TEMPLATES: dict[str, list[str]] = {
    "usual": [
        "Hey {first}! {agent} from {restaurant}. It's been {gap} since your {usual}. Want me to get one going for pickup around {when}? Just reply here or {call}.",
        "hey {first}, {agent} at {restaurant} here. {day_line}Your usual {usual} sound good tonight? I can have it ready by {when}. Reply here or {call}.",
        "Hi {first}, it's {agent} from {restaurant}. Haven't seen you in {gap}! Want your {usual} for around {when}? Text back or {call}.",
    ],
    "win_back": [
        "Hey {first}, {agent} from {restaurant}. It's been {gap}, we miss you around here! Want me to put in a {usual} for {when}? Reply or {call}.",
        "{first}! {agent} at {restaurant}. Been {gap}. Your {usual} is calling 😄 want one ready around {when}? Reply here or {call}.",
    ],
    "second_order": [
        "Hi {first}, {agent} from {restaurant} here. Hope you liked the {usual} last time! Want another one ready around {when}? Reply here or {call}.",
        "Hey {first}! {agent} at {restaurant}. Thanks again for ordering with us. Up for another {usual}? I can have it ready by {when}. Reply or {call}.",
    ],
}

NUDGE_TEMPLATES = [
    "No rush, still want me to put that in for {when}?",
    "Hey, just checking, want me to send that order through for {when}?",
    "Still good for {when}? Just say yes and I'll get it going.",
]
