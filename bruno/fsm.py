"""Dialogue finite-state machine.

The LLM never moves the conversation on its own: it reports an *intent* and a
proposed cart, and `next_state` decides the transition deterministically.
In particular, an order can only reach POS injection from
STATE_CONFIRMING_PAYMENT, i.e. after the customer has been shown the exact
items, total and payment method and has explicitly said yes to that.
"""

from enum import StrEnum


class State(StrEnum):
    INITIAL_OUTREACH = "STATE_INITIAL_OUTREACH"
    AWAITING_RESPONSE = "STATE_AWAITING_RESPONSE"
    NEGOTIATING_MODIFICATIONS = "STATE_NEGOTIATING_MODIFICATIONS"
    CONFIRMING_PAYMENT = "STATE_CONFIRMING_PAYMENT"
    AWAITING_PAYMENT = "STATE_AWAITING_PAYMENT"  # pay link sent, waiting on Stripe
    POS_INJECTION = "STATE_POS_INJECTION"
    # Terminal states
    COMPLETED = "STATE_COMPLETED"
    DECLINED = "STATE_DECLINED"
    OPTED_OUT = "STATE_OPTED_OUT"
    FAILED = "STATE_FAILED"


class Intent(StrEnum):
    UPDATE_ORDER = "update_order"  # accepted the usual, or changed items/time
    CONFIRM_ORDER = "confirm_order"  # explicit "yes, place it"
    DECLINE = "decline"  # not today / no thanks
    QUESTION = "question"  # asked something, no order change
    HANDOFF = "handoff"  # complaint, refund, allergy question, wants a person
    OTHER = "other"


TERMINAL = {State.COMPLETED, State.DECLINED, State.OPTED_OUT, State.FAILED}

_ACTIVE_EXITS = {State.DECLINED, State.OPTED_OUT}

TRANSITIONS: dict[State, set[State]] = {
    State.INITIAL_OUTREACH: {State.AWAITING_RESPONSE, *_ACTIVE_EXITS},
    State.AWAITING_RESPONSE: {
        State.AWAITING_RESPONSE,
        State.NEGOTIATING_MODIFICATIONS,
        State.CONFIRMING_PAYMENT,
        *_ACTIVE_EXITS,
    },
    State.NEGOTIATING_MODIFICATIONS: {
        State.NEGOTIATING_MODIFICATIONS,
        State.CONFIRMING_PAYMENT,
        *_ACTIVE_EXITS,
    },
    State.CONFIRMING_PAYMENT: {
        State.CONFIRMING_PAYMENT,
        State.NEGOTIATING_MODIFICATIONS,
        State.AWAITING_PAYMENT,
        State.POS_INJECTION,
        *_ACTIVE_EXITS,
    },
    State.AWAITING_PAYMENT: {
        State.AWAITING_PAYMENT,
        State.CONFIRMING_PAYMENT,
        State.NEGOTIATING_MODIFICATIONS,
        State.POS_INJECTION,
        *_ACTIVE_EXITS,
    },
    State.POS_INJECTION: {State.COMPLETED, State.FAILED},
    State.COMPLETED: set(),
    State.DECLINED: set(),
    State.OPTED_OUT: set(),
    State.FAILED: set(),
}


class InvalidTransition(Exception):
    pass


def assert_transition(current: State, target: State) -> State:
    if target not in TRANSITIONS[current]:
        raise InvalidTransition(f"{current} -> {target} is not allowed")
    return target


def next_state(
    current: State,
    intent: Intent,
    *,
    cart_ready: bool,
    cart_changed: bool,
) -> State:
    """Pick the next state after a customer turn.

    cart_ready:   cart is non-empty and passed menu validation.
    cart_changed: this turn altered the cart (items, sizes, mods or quantity).
    """
    if intent == Intent.DECLINE:
        return State.DECLINED

    if intent == Intent.HANDOFF:
        return current  # a person takes it from here; the order state doesn't move

    if current == State.AWAITING_PAYMENT:
        # Only a paid Stripe webhook moves this forward; changes need a new recap.
        if cart_changed:
            return State.CONFIRMING_PAYMENT if cart_ready else State.NEGOTIATING_MODIFICATIONS
        return State.AWAITING_PAYMENT

    if (
        intent == Intent.CONFIRM_ORDER
        and current == State.CONFIRMING_PAYMENT
        and cart_ready
        and not cart_changed
    ):
        return State.POS_INJECTION

    if cart_ready and (cart_changed or intent in (Intent.UPDATE_ORDER, Intent.CONFIRM_ORDER)):
        # Show the priced summary and ask for an explicit yes.
        return State.CONFIRMING_PAYMENT

    if current == State.CONFIRMING_PAYMENT and cart_ready:
        # Question while a summary is pending: stay put, the summary still stands.
        return State.CONFIRMING_PAYMENT

    if current == State.AWAITING_RESPONSE and intent in (Intent.QUESTION, Intent.OTHER):
        return State.AWAITING_RESPONSE

    return State.NEGOTIATING_MODIFICATIONS
