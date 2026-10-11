"""Bruno's core engine. Text and voice both run through this class, against the
same Conversation row, so the customer's cart and state follow them across
channels."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from bruno import cadence
from bruno.deps import Deps
from bruno.fsm import Intent, State, assert_transition, next_state
from bruno.llm import AgentTurn, LLMError, TurnContext
from bruno.menu import CartItem, ValidatedCart, money
from bruno.messaging import MessagingError, normalize_phone
from bruno.models import Conversation, Customer, Message, Order
from bruno.orchestrator import (
    CustomerContext,
    build_context,
    compose_outreach,
    confirmation_request,
    order_summary,
    propose_pickup,
    system_prompt,
)
from bruno.pos import POSCustomer, POSOrder, POSPayment
from bruno.timeutil import display_time, resolve_pickup, to_local, utcnow
from bruno.weather import current_weather

log = logging.getLogger(__name__)

OPT_OUT_KEYWORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout", "revoke"}
OPT_IN_KEYWORDS = {"start", "unstop", "subscribe"}
HISTORY_LIMIT = 20


@dataclass
class TurnOutcome:
    reply: str | None
    state: State | None
    conversation_id: int | None = None
    order_id: int | None = None


@dataclass
class FinalizeResult:
    ok: bool
    message: str
    order: Order | None = None


def _cart_key(items: list[CartItem]) -> list[tuple]:
    return sorted(i.key() for i in items)


class Bruno:
    def __init__(self, session: Session, deps: Deps, clock: Callable[[], datetime] = utcnow):
        self.db = session
        self.deps = deps
        self.settings = deps.settings
        self.menu = deps.menu
        self.clock = clock

    # ------------------------------------------------------------------ helpers
    def get_or_create_customer(self, phone: str) -> Customer:
        phone = normalize_phone(phone)
        customer = self.db.scalars(select(Customer).where(Customer.phone_number == phone)).first()
        if not customer:
            customer = Customer(phone_number=phone)
            self.db.add(customer)
            self.db.flush()
        return customer

    def _record(self, conv: Conversation, role: str, channel: str, content: str, provider_id: str | None = None) -> None:
        self.db.add(
            Message(
                conversation_id=conv.id, role=role, channel=channel, content=content, provider_message_id=provider_id
            )
        )

    def _set_state(self, conv: Conversation, target: State) -> None:
        conv.state = assert_transition(State(conv.state), target).value
        conv.updated_at = self.clock()

    def _context(self, customer: Customer, conv: Conversation | None) -> CustomerContext:
        return build_context(customer, conv, self.menu, self.settings, self.clock(), current_weather(self.settings))

    def _active_conversation(self, customer: Customer, channel: str) -> Conversation:
        conv = cadence.open_conversation(self.db, customer.id, self.settings, self.clock())
        if conv is None:
            conv = Conversation(
                customer_id=customer.id,
                state=State.AWAITING_RESPONSE.value,
                cart_json=[],
                origin="inbound",
                last_channel=channel,
            )
            self.db.add(conv)
            self.db.flush()
        elif conv.state == State.INITIAL_OUTREACH.value:
            self._set_state(conv, State.AWAITING_RESPONSE)
        conv.last_channel = channel
        return conv

    def _send(self, customer: Customer, text: str) -> str | None:
        return self.deps.messenger.send(customer.phone_number, text)

    # ------------------------------------------------------------------ outreach
    def start_outreach(self, customer: Customer) -> Conversation | None:
        """Text a drifting customer their usual. Returns None if they can't be contacted."""
        if customer.opted_out:
            return None
        now = self.clock()
        tracker = customer.cadence or cadence.recompute_customer(self.db, customer, self.settings, now)
        if not tracker.usual_order_json:
            return None
        pickup = propose_pickup(tracker, now, self.settings)
        conv = Conversation(
            customer_id=customer.id,
            state=State.INITIAL_OUTREACH.value,
            cart_json=tracker.usual_order_json,
            pickup_time=pickup,
            origin="outreach",
            last_channel="text",
        )
        self.db.add(conv)
        self.db.flush()
        ctx = self._context(customer, conv)
        text = compose_outreach(ctx, pickup, self.settings, now)
        try:
            provider_id = self._send(customer, text)
        except MessagingError as exc:
            log.error("outreach to customer %s failed: %s", customer.id, exc)
            self._set_state(conv, State.DECLINED)
            self._record(conv, "system", "text", f"Outreach send failed: {exc}")
            return None
        self._record(conv, "agent", "text", text, provider_id)
        self._set_state(conv, State.AWAITING_RESPONSE)
        tracker.last_outreach_at = now
        return conv

    # ------------------------------------------------------------------ inbound text
    def handle_inbound_text(self, phone: str, text: str, provider_message_id: str | None = None) -> TurnOutcome:
        if provider_message_id and self.db.scalars(
            select(Message.id).where(Message.provider_message_id == provider_message_id)
        ).first():
            return TurnOutcome(reply=None, state=None)  # webhook retry: already handled

        customer = self.get_or_create_customer(phone)
        keyword = text.strip().lower().rstrip(".!")

        if keyword in OPT_OUT_KEYWORDS:
            customer.opted_out = True
            conv = cadence.open_conversation(self.db, customer.id, self.settings, self.clock())
            reply = f"You're unsubscribed from {self.settings.restaurant_name} texts. Reply START to resubscribe."
            if conv:
                self._record(conv, "customer", "text", text, provider_message_id)
                self._set_state(conv, State.OPTED_OUT)
                self._record(conv, "agent", "text", reply)
            self._send(customer, reply)
            return TurnOutcome(reply=reply, state=State.OPTED_OUT, conversation_id=conv.id if conv else None)

        if keyword in OPT_IN_KEYWORDS and customer.opted_out:
            customer.opted_out = False
            reply = f"You're resubscribed to {self.settings.restaurant_name} texts. Hungry? Just text me your order!"
            self._send(customer, reply)
            return TurnOutcome(reply=reply, state=None)

        conv = self._active_conversation(customer, "text")
        self._record(conv, "customer", "text", text, provider_message_id)
        self.db.flush()
        reply, order = self._run_text_turn(conv, customer, text)
        self._record(conv, "agent", "text", reply)
        try:
            self._send(customer, reply)
        except MessagingError as exc:
            log.error("reply to customer %s failed: %s", customer.id, exc)
        return TurnOutcome(
            reply=reply, state=State(conv.state), conversation_id=conv.id, order_id=order.id if order else None
        )

    def _history(self, conv: Conversation) -> list[dict]:
        msgs = [m for m in conv.messages if m.role in ("customer", "agent")][-HISTORY_LIMIT:]
        return [
            {"role": "user" if m.role == "customer" else "assistant", "content": m.content}
            for m in msgs
        ]

    def _generate(self, tctx: TurnContext) -> tuple[AgentTurn | None, ValidatedCart | None]:
        turn: AgentTurn | None = None
        validated: ValidatedCart | None = None
        for _ in range(2):  # one corrective retry if the cart fails menu validation
            try:
                turn = self.deps.llm.generate_turn(tctx)
            except LLMError as exc:
                log.error("LLM failure: %s", exc)
                return (turn, validated) if turn else (None, None)
            validated = self.menu.validate(turn.cart)
            if not validated.errors or turn.intent == Intent.DECLINE:
                break
            tctx.corrections = validated.errors
        return turn, validated

    def _run_text_turn(self, conv: Conversation, customer: Customer, text: str) -> tuple[str, Order | None]:
        self.db.refresh(conv)
        ctx = self._context(customer, conv)
        current = State(conv.state)
        tctx = TurnContext(
            system_prompt=system_prompt(ctx, self.menu, self.settings),
            history=self._history(conv),
            customer_message=text,
            current_cart=ctx.cart,
            usual_order=ctx.usual,
            menu=self.menu,
            state=current,
            pickup_time=conv.pickup_time,
        )
        turn, validated = self._generate(tctx)
        if turn is None or validated is None:
            call = self.settings.voice_phone_number or "this number"
            return (f"Sorry, I'm having a little trouble on my end. Give us a call at {call} and we'll get you sorted!", None)

        before = ctx.validated.items()
        after = validated.items()
        new_pickup = conv.pickup_time
        if turn.pickup_time:
            new_pickup = resolve_pickup(
                turn.pickup_time, self.clock(), self.settings.restaurant_timezone, self.settings.min_prep_minutes
            )
        changed = _cart_key(before) != _cart_key(after) or new_pickup != conv.pickup_time
        target = next_state(current, turn.intent, cart_ready=validated.ready, cart_changed=changed)

        if target != State.DECLINED:
            conv.cart_json = [i.model_dump() for i in after]
            if after and not new_pickup:
                new_pickup = resolve_pickup(
                    None, self.clock(), self.settings.restaurant_timezone, self.settings.min_prep_minutes
                )
            conv.pickup_time = new_pickup
        self._set_state(conv, target)

        reply = turn.reply.strip()
        if target == State.POS_INJECTION:
            result = self.finalize_order(conv, "text")
            return f"{reply} {result.message}".strip(), result.order
        if target == State.CONFIRMING_PAYMENT:
            if changed or current != State.CONFIRMING_PAYMENT:
                summary = confirmation_request(validated, conv.pickup_time, ctx.payment_label)
                reply = f"{reply}\n\n{summary}".strip()
            elif "yes" not in reply.lower():
                reply = f"{reply} Just reply YES whenever you're ready and I'll put it in.".strip()
        elif validated.errors and "?" not in reply:
            reply = f"{reply} Quick question: {validated.errors[0]}".strip()
        return reply, None

    # ------------------------------------------------------------------ fulfillment
    def finalize_order(self, conv: Conversation, channel: str) -> FinalizeResult:
        """Charge + inject into the POS. Only callable from STATE_POS_INJECTION."""
        if State(conv.state) != State.POS_INJECTION:
            raise RuntimeError(f"finalize_order called in {conv.state}")
        customer = self.db.get(Customer, conv.customer_id)
        validated = self.menu.validate(conv.cart_json)
        call = self.settings.voice_phone_number or "this number"
        if not validated.ready:
            self._set_state(conv, State.FAILED)
            return FinalizeResult(False, f"Something's off with that order. Give us a call at {call}?")

        now = self.clock()
        tz = self.settings.restaurant_timezone
        pickup = resolve_pickup(conv.pickup_time, now, tz, self.settings.min_prep_minutes)
        conv.pickup_time = pickup
        local_now = to_local(now, tz)
        h, m = map(int, pickup.split(":"))
        pickup_at = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
        external_id = f"bruno-conv-{conv.id}"

        payment = self.deps.payments.charge(customer, validated.total_cents, external_id)
        if not payment.ok:
            self._set_state(conv, State.FAILED)
            self._record(conv, "system", channel, f"Payment failed: {payment.error}")
            return FinalizeResult(False, f"Hmm, the card on file didn't go through. Give us a call at {call} and we'll sort it out!")

        pos_order = POSOrder(
            external_id=external_id,
            restaurant=self.settings.restaurant_name,
            channel=channel,
            customer=POSCustomer(name=customer.name or f"Guest {customer.phone_number[-4:]}", phone=customer.phone_number),
            pickup_at=pickup_at,
            line_items=validated.lines,
            subtotal_cents=validated.subtotal_cents,
            tax_cents=validated.tax_cents,
            total_cents=validated.total_cents,
            payment=POSPayment(status=payment.status, method=payment.method, reference=payment.reference),
            created_at=local_now,
        )
        try:
            result = self.deps.pos.inject(pos_order)
        except Exception as exc:
            log.exception("POS injection failed for conversation %s", conv.id)
            if payment.reference:
                self.deps.payments.refund(payment.reference)
            self._set_state(conv, State.FAILED)
            self._record(conv, "system", channel, f"POS injection failed: {exc}")
            return FinalizeResult(
                False, f"Ugh, our system hiccuped putting that in and you weren't charged. Give us a call at {call} and we'll get it going!"
            )

        order = Order(
            customer_id=customer.id,
            items_json=conv.cart_json,
            total_amount=Decimal(validated.total_cents) / 100,
            order_date=now,
            channel=channel,
            status="completed",
            pickup_time=pickup,
            pos_order_id=result.pos_order_id,
            external_id=external_id,
            payment_reference=payment.reference,
            conversation_id=conv.id,
        )
        self.db.add(order)
        self._set_state(conv, State.COMPLETED)
        self.db.flush()
        cadence.recompute_customer(self.db, customer, self.settings, now)

        if payment.status == "captured":
            paid = f"Charged {money(validated.total_cents)} to your {payment.method}."
        else:
            paid = f"Total is {money(validated.total_cents)}, pay when you pick up."
        return FinalizeResult(
            True,
            f"You're all set{', ' + customer.first_name if customer.name else ''}! "
            f"Ready around {display_time(pickup)}. {paid} See you soon 🍕",
            order,
        )

    # ------------------------------------------------------------------ voice tools
    def voice_context(self, phone: str) -> CustomerContext:
        customer = self.get_or_create_customer(phone)
        conv = cadence.open_conversation(self.db, customer.id, self.settings, self.clock())
        return self._context(customer, conv)

    def voice_update_cart(self, phone: str, items: list[dict], pickup_time: str | None) -> dict:
        customer = self.get_or_create_customer(phone)
        conv = self._active_conversation(customer, "voice")
        validated = self.menu.validate(items)
        current = State(conv.state)
        if pickup_time:
            conv.pickup_time = resolve_pickup(
                pickup_time, self.clock(), self.settings.restaurant_timezone, self.settings.min_prep_minutes
            )
        elif not conv.pickup_time:
            conv.pickup_time = resolve_pickup(
                None, self.clock(), self.settings.restaurant_timezone, self.settings.min_prep_minutes
            )
        conv.cart_json = [i.model_dump() for i in validated.items()]
        target = next_state(current, Intent.UPDATE_ORDER, cart_ready=validated.ready, cart_changed=True)
        self._set_state(conv, target)
        label = self._context(customer, conv).payment_label
        self._record(conv, "system", "voice", f"Voice cart update: {self.menu.describe(conv.cart_json) or 'empty'}")
        return {
            "ok": validated.ready,
            "errors": validated.errors,
            "summary": order_summary(validated, conv.pickup_time, label) if validated.lines else "",
            "total": money(validated.total_cents),
            "pickup_time": display_time(conv.pickup_time),
            "payment_method": label,
            "next_step": (
                "Read the items, total and pickup time back to the caller and ask them to confirm. "
                "Only call place_order after they clearly say yes."
                if validated.ready
                else "Ask the caller to clarify the problems listed in errors, then call update_cart again."
            ),
        }

    def voice_place_order(self, phone: str, customer_confirmed: bool) -> dict:
        customer = self.get_or_create_customer(phone)
        conv = cadence.open_conversation(self.db, customer.id, self.settings, self.clock())
        if not customer_confirmed:
            return {"ok": False, "message": "The caller has not confirmed yet. Read the summary back and ask."}
        if conv is None or State(conv.state) != State.CONFIRMING_PAYMENT:
            return {"ok": False, "message": "No confirmed order summary yet. Call update_cart first and read it back."}
        conv.last_channel = "voice"
        self._set_state(conv, State.POS_INJECTION)
        result = self.finalize_order(conv, "voice")
        self._record(conv, "system", "voice", f"Voice place_order: {result.message}")
        return {"ok": result.ok, "message": result.message, "order_id": result.order.id if result.order else None}
