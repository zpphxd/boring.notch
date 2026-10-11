"""Deterministic, offline stand-in for the LLM.

Handles the common re-order phrasings ("yes", "swap pepperoni for mushroom",
"add a diet coke", "make it a medium", "around 6:30", "deliver to 12 Main St",
"no thanks") using the menu's aliases. It powers the tests and lets the whole
product run before any API key is entered; free-form writing (outreach copy)
falls back to Bruno's built-in templates.
"""

import re

from bruno.fsm import Intent
from bruno.llm.base import AgentTurn, LLMError, TurnContext
from bruno.menu import CartItem, Menu

YES = re.compile(
    r"^(y|yes|yep|yeah|ya|yup|sure|ok|okay|k|perfect|sounds good|do it|let'?s do it|put it in|send it|"
    r"place it|confirm|go for it|please|absolutely|the usual|usual|yes please|bet|fire it)\b"
)
NO = re.compile(r"^(no|nah|nope|not today|not tonight|no thanks|pass|maybe next time|i'?m good)\b")
TIME = re.compile(r"\b(?:at|around|by|for|@)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b")
HANDOFF = re.compile(
    r"(talk|speak) to (a |the )?(person|human|manager|someone|real)|refund|complain|wrong order|"
    r"was cold|allerg|are you a (bot|robot|real)",
)
ADDRESS = re.compile(
    r"\b(\d{1,6}\s+[\w .'-]+?\b(?:st|street|ave|avenue|rd|road|blvd|ln|lane|dr|drive|way|ct|court|pl|place|pkwy)\b\.?"
    r"(?:[\w ,#.-]*)?)",
    re.I,
)
REMEMBER = re.compile(r"\b(i (?:like|love|prefer) (?:it |them |my pizza )?[\w -]+|always [\w -]+)")
NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "another": 1}


class MockLLM:
    name = "mock"

    def write(self, system: str, prompt: str) -> str:
        raise LLMError("mock model does not write free text; using templates")

    def generate_turn(self, ctx: TurnContext) -> AgentTurn:
        menu = ctx.menu
        text = ctx.customer_message.lower().strip()

        if HANDOFF.search(text):
            return AgentTurn(
                intent=Intent.HANDOFF, cart=ctx.current_cart,
                reply="Totally get it. Let me grab someone from the team, they'll text you back here in a few.",
            )

        cart = [c.model_copy(deep=True) for c in ctx.current_cart]
        base_from_usual = not cart and bool(ctx.usual_order)
        if base_from_usual:
            cart = [c.model_copy(deep=True) for c in ctx.usual_order]

        if NO.match(text) and not menu.find(text):
            return AgentTurn(
                intent=Intent.DECLINE, cart=ctx.current_cart,
                reply="No worries at all! We'll be here whenever the craving hits 🍕",
            )

        address_match = ADDRESS.search(ctx.customer_message)
        address = address_match.group(1).strip(" .") if address_match else None
        order_text = text.replace(address.lower(), " ") if address else text

        changed = False
        for clause in re.split(r",|;|!|\.(?!\d)|\band (?=(?:then|add|swap|switch|make|remove|drop|plus)\b)", order_text):
            changed |= _apply_clause(clause.strip(), cart, menu)

        pickup = _parse_time(order_text)
        fulfillment = None
        if re.search(r"\bdeliver(y|ed)?\b|\bbring it\b", text):
            fulfillment = "delivery"
        elif re.search(r"\bpick ?(it )?up\b|\bpickup\b", text):
            fulfillment = "pickup"
        if address and not fulfillment:
            fulfillment = "delivery"
        remember = [m.group(1).strip() for m in REMEMBER.finditer(text)][:1]

        def turn(intent: Intent, c: list[CartItem], reply: str) -> AgentTurn:
            return AgentTurn(
                intent=intent, cart=c, pickup_time=pickup, fulfillment=fulfillment,
                delivery_address=address, remember=remember, reply=reply,
            )

        if changed or (base_from_usual and (pickup or fulfillment or re.search(r"\busual\b", text))):
            return turn(Intent.UPDATE_ORDER, cart, "You got it!")
        if YES.match(text):
            return turn(Intent.CONFIRM_ORDER, cart, "Awesome!")
        if pickup or fulfillment or address or remember:
            return turn(Intent.UPDATE_ORDER, ctx.current_cart, "Done.")
        return turn(Intent.QUESTION, ctx.current_cart, "Happy to help! What can I get started for you?")


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
        found = menu.find(remove.group(1))
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

    add = re.search(r"\b(?:add|plus|throw in|and|get|want|grab)\s+(.+)", clause)
    if add:
        changed = False
        for seg in re.split(r"\band\b", add.group(1)):
            changed |= _add(seg.strip(), cart, menu)
        return changed
    return False


def _swap(old_text: str, new_text: str, cart: list[CartItem], menu: Menu) -> bool:
    old, new = menu.find(old_text), menu.find(new_text)
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
    found = menu.find(segment)
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
    qty_match = re.match(r"(?:me |us )?(\d+|a|an|one|two|three|four|another)\b", segment)
    qty = 1
    if qty_match:
        word = qty_match.group(1)
        qty = int(word) if word.isdigit() else NUMBERS[word]
    parsed = menu.parse_item_text(segment, qty)
    item = parsed if parsed and parsed.item_id == ident else CartItem(item_id=ident, quantity=qty)
    if menu.items[ident].sizes and not item.size:
        item.size = "large"
    for c in cart:
        if c.item_id == item.item_id and c.size == item.size and c.modifiers == item.modifiers:
            c.quantity += item.quantity
            return True
    cart.append(item)
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
