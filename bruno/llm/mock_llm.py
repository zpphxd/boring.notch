"""Deterministic, offline stand-in for the LLM.

Handles the common re-order phrasings ("yes", "swap pepperoni for mushroom",
"add a diet coke", "make it a medium", "around 6:30", "no thanks") using the
menu's aliases. Used by tests and for running the demo without API keys.
"""

import re

from bruno.fsm import Intent
from bruno.llm.base import AgentTurn, TurnContext
from bruno.menu import SIZES, CartItem, Menu

YES = re.compile(
    r"^(y|yes|yep|yeah|ya|yup|sure|ok|okay|k|perfect|sounds good|do it|let'?s do it|"
    r"place it|confirm|go for it|please|absolutely|the usual|usual|yes please)\b"
)
NO = re.compile(r"^(no|nah|nope|not today|not tonight|no thanks|pass|maybe next time|i'?m good)\b")
TIME = re.compile(r"\b(?:at|around|by|for|@)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b")
NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "another": 1}


class MockLLM:
    name = "mock"

    def generate_turn(self, ctx: TurnContext) -> AgentTurn:
        menu = ctx.menu
        text = ctx.customer_message.lower().strip()
        cart = [c.model_copy(deep=True) for c in ctx.current_cart]
        base_from_usual = not cart and bool(ctx.usual_order)
        if base_from_usual:
            cart = [c.model_copy(deep=True) for c in ctx.usual_order]

        if NO.match(text) and not _find_kind(text, menu):
            return AgentTurn(
                intent=Intent.DECLINE, cart=ctx.current_cart, pickup_time=None,
                reply="No worries at all! We'll be here whenever the craving hits 🍕",
            )

        changed = False
        for clause in re.split(r",|;|!|\.(?!\d)|\band (?=(?:then|add|swap|switch|make|remove|drop|plus)\b)", text):
            changed |= _apply_clause(clause.strip(), cart, menu)

        pickup = _parse_time(text)

        if changed or (base_from_usual and (pickup or re.search(r"\busual\b", text))):
            return AgentTurn(intent=Intent.UPDATE_ORDER, cart=cart, pickup_time=pickup, reply="You got it!")
        if YES.match(text):
            return AgentTurn(intent=Intent.CONFIRM_ORDER, cart=cart, pickup_time=pickup, reply="Awesome!")
        if pickup:
            return AgentTurn(
                intent=Intent.UPDATE_ORDER, cart=ctx.current_cart, pickup_time=pickup, reply="Done, updated the time."
            )
        return AgentTurn(
            intent=Intent.QUESTION, cart=ctx.current_cart, pickup_time=None,
            reply="Happy to help! What can I get started for you?",
        )


def _aliases(menu: Menu) -> list[tuple[str, str, str]]:
    out = [(a, "item", i.id) for i in menu.items.values() for a in i.aliases]
    out += [(a, "mod", m.id) for m in menu.modifiers.values() for a in m.aliases]
    return sorted(out, key=lambda t: -len(t[0]))


def _find(segment: str, menu: Menu, kind: str | None = None) -> str | None:
    for alias, k, ident in _aliases(menu):
        if (kind is None or k == kind) and re.search(rf"\b{re.escape(alias)}\b", segment):
            return ident
    return None


def _find_kind(segment: str, menu: Menu) -> tuple[str, str] | None:
    for alias, k, ident in _aliases(menu):
        if re.search(rf"\b{re.escape(alias)}\b", segment):
            return k, ident
    return None


def _pizzas(cart: list[CartItem], menu: Menu) -> list[CartItem]:
    return [c for c in cart if menu.items[c.item_id].category == "pizza"]


def _apply_clause(clause: str, cart: list[CartItem], menu: Menu) -> bool:
    if not clause:
        return False

    swap = re.search(r"\b(?:swap|switch|change|replace|sub)\s+(?:the\s+|out\s+)?(.+?)\s+(?:for|with|to)\s+(.+)", clause)
    if swap:
        return _swap(swap.group(1), swap.group(2), cart, menu)
    instead = re.search(r"(.+?)\s+instead of\s+(.+)", clause)
    if instead:
        return _swap(instead.group(2), instead.group(1), cart, menu)

    size = re.search(r"\bmake (?:it|that|the pizza) (?:a )?(small|medium|large)\b", clause)
    if size:
        for p in _pizzas(cart, menu):
            p.size = size.group(1)
        return bool(_pizzas(cart, menu))

    remove = re.search(r"\b(?:remove|drop|without|hold the|no more|minus|skip the)\s+(?:the\s+)?(.+)", clause)
    if remove:
        found = _find_kind(remove.group(1), menu)
        if not found:
            return False
        kind, ident = found
        if kind == "mod":
            hit = False
            for p in cart:
                if ident in p.modifiers:
                    p.modifiers.remove(ident)
                    hit = True
            return hit
        before = len(cart)
        cart[:] = [c for c in cart if c.item_id != ident]
        return len(cart) != before

    add = re.search(r"\b(?:add|plus|throw in|and)\s+(.+)", clause)
    if add:
        changed = False
        for seg in re.split(r"\band\b", add.group(1)):
            changed |= _add(seg.strip(), cart, menu)
        return changed
    return False


def _swap(old_text: str, new_text: str, cart: list[CartItem], menu: Menu) -> bool:
    old, new = _find_kind(old_text, menu), _find_kind(new_text, menu)
    if not old or not new:
        return False
    if old[0] == "mod" and new[0] == "mod":
        hit = False
        for p in cart:
            if old[1] in p.modifiers:
                p.modifiers[p.modifiers.index(old[1])] = new[1]
                hit = True
        return hit
    if old[0] == "item" and new[0] == "item":
        for c in cart:
            if c.item_id == old[1]:
                c.item_id = new[1]
                c.modifiers = [m for m in c.modifiers if menu.modifiers[m].group in menu.items[new[1]].modifier_groups]
                if not menu.items[new[1]].sizes:
                    c.size = None
                return True
    return False


def _add(segment: str, cart: list[CartItem], menu: Menu) -> bool:
    found = _find_kind(segment, menu)
    if not found:
        return False
    kind, ident = found
    if kind == "mod":
        pizzas = [p for p in _pizzas(cart, menu) if menu.modifiers[ident].group in menu.items[p.item_id].modifier_groups]
        if not pizzas:
            return False
        if ident not in pizzas[0].modifiers:
            pizzas[0].modifiers.append(ident)
        return True
    qty_match = re.match(r"(\d+|a|an|one|two|three|four|another)\b", segment)
    qty = 1
    if qty_match:
        word = qty_match.group(1)
        qty = int(word) if word.isdigit() else NUMBERS[word]
    size = next((s for s in SIZES if re.search(rf"\b{s}\b", segment)), None)
    if menu.items[ident].sizes and not size:
        size = "large"
    for c in cart:
        if c.item_id == ident and c.size == size and not c.modifiers:
            c.quantity += qty
            return True
    cart.append(CartItem(item_id=ident, size=size, quantity=qty))
    return True


def _parse_time(text: str) -> str | None:
    m = TIME.search(text)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if hour > 12 or minute > 59:
        return None
    if ampm == "pm" or (ampm is None and hour < 11):
        hour = hour % 12 + 12
    elif ampm == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"
