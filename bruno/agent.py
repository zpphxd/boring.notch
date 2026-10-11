"""Bruno's core engine. Text and voice both run through this class, against the
same Conversation row, so the customer's cart and state follow them across
channels."""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from bruno import cadence, humanize, learning
from bruno.delivery import DeliveryError, marketplace_links
from bruno.deps import Deps
from bruno.fsm import Intent, State, assert_transition, next_state
from bruno.llm import AgentTurn, LLMError, TurnContext
from bruno.menu import CartItem, ValidatedCart, money
from bruno.messaging import MessagingError, normalize_phone
from bruno.models import Conversation, Customer, Message, Order, Outreach
from bruno.orchestrator import (
    CustomerContext,
    build_context,
    order_summary,
    order_totals,
    propose_pickup,
    recap_facts,
    system_prompt,
)
from bruno.payments import PaymentError, PaymentResult
from bruno.pos import POSCustomer, POSOrder, POSPayment
from bruno.timeutil import display_local, display_time, resolve_pickup, to_local, utcnow
from bruno.weather import current_weather

log = logging.getLogger(__name__)

OPT_OUT_KEYWORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout", "revoke"}
OPT_IN_KEYWORDS = {"start", "unstop", "subscribe"}
HISTORY_LIMIT = 24
MAX_MEMORY = 15
QUOTE_TTL = timedelta(minutes=10)


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


@dataclass
class FulfillmentDecision:
    fulfillment: str
    address: str | None
    note: str | None = None  # extra line for the customer (links, problems)
    blocked: bool = False  # order can't be confirmed yet
    eta: str | None = None  # display time of delivery drop-off


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
            customer = Customer(phone_number=phone, memory=[])
            self.db.add(customer)
            self.db.flush()
        return customer

    def _record(self, conv: Conversation, role: str, channel: str, content: str, provider_id: str | None = None) -> None:
        self.db.add(
            Message(
                conversation_id=conv.id, role=role, channel=channel, content=content,
                provider_message_id=provider_id, created_at=self.clock(),
            )
        )

    def _ready(self, hhmm: str | None) -> str:
        s = self.settings
        return resolve_pickup(hhmm, self.clock(), s.restaurant_timezone, s.min_prep_minutes, s.opening_time)

    def _set_state(self, conv: Conversation, target: State) -> None:
        conv.state = assert_transition(State(conv.state), target).value
        conv.updated_at = self.clock()

    def _context(self, customer: Customer, conv: Conversation | None, addon: str | None = None) -> CustomerContext:
        return build_context(
            customer, conv, self.menu, self.settings, self.clock(), current_weather(self.settings), addon
        )

    def _active_conversation(self, customer: Customer, channel: str) -> Conversation:
        conv = cadence.open_conversation(self.db, customer.id, self.settings, self.clock())
        if conv is None:
            conv = Conversation(
                customer_id=customer.id,
                state=State.AWAITING_RESPONSE.value,
                cart_json=[],
                origin="inbound",
                last_channel=channel,
                created_at=self.clock(),
                updated_at=self.clock(),
            )
            self.db.add(conv)
            self.db.flush()
        elif conv.state == State.INITIAL_OUTREACH.value:
            self._set_state(conv, State.AWAITING_RESPONSE)
        conv.last_channel = channel
        conv.updated_at = self.clock()
        return conv

    def send(self, phone: str, text: str) -> list[str | None]:
        """Send like a person: typing bubble, a pause, maybe two short texts."""
        messenger = self.deps.messenger
        return humanize.deliver(
            lambda bubble: messenger.send(phone, bubble),
            text,
            typing=lambda: messenger.typing(phone),
            human_delays=self.settings.human_delays,
            sleep=self.deps.sleep,
        )

    def notify_owner(self, text: str) -> None:
        if not self.settings.owner_phone:
            return
        try:
            self.deps.messenger.send(normalize_phone(self.settings.owner_phone), text)
        except MessagingError as exc:
            log.error("owner notification failed: %s", exc)

    def remember(self, customer: Customer, facts: list[str]) -> None:
        memory = list(customer.memory or [])
        for fact in facts:
            fact = fact.strip().rstrip(".")
            if fact and fact.lower() not in {m.lower() for m in memory}:
                memory.append(fact[:120])
        customer.memory = memory[-MAX_MEMORY:]

    # ------------------------------------------------------------------ outreach
    def open_outreach_conversation(self, customer: Customer, outreach: Outreach, pickup: str) -> Conversation | None:
        """Send an already-written outreach text and open the conversation for it."""
        now = self.clock()
        tracker = customer.cadence
        conv = Conversation(
            customer_id=customer.id,
            state=State.INITIAL_OUTREACH.value,
            cart_json=tracker.usual_order_json if tracker else [],
            pickup_time=pickup,
            origin="outreach",
            outreach_id=outreach.id,
            last_channel="text",
            created_at=now,
            updated_at=now,
        )
        self.db.add(conv)
        self.db.flush()
        try:
            ids = self.send(customer.phone_number, outreach.message)
        except MessagingError as exc:
            log.error("outreach to customer %s failed: %s", customer.id, exc)
            self._set_state(conv, State.DECLINED)
            self._record(conv, "system", "text", f"Outreach send failed: {exc}")
            outreach.status = "failed"
            return None
        self._record(conv, "agent", "text", outreach.message, ids[0] if ids else None)
        self._set_state(conv, State.AWAITING_RESPONSE)
        local = to_local(now, self.settings.restaurant_timezone)
        outreach.status = "sent"
        outreach.sent_at = now
        outreach.send_hour = local.hour
        outreach.weekday = local.weekday()
        if tracker:
            tracker.last_outreach_at = now
        return conv

    # ------------------------------------------------------------------ inbound text
    def handle_inbound_text(self, phone: str, text: str, provider_message_id: str | None = None) -> TurnOutcome:
        if provider_message_id and self.db.scalars(
            select(Message.id).where(Message.provider_message_id == provider_message_id)
        ).first():
            return TurnOutcome(reply=None, state=None)  # webhook retry: already handled

        now = self.clock()
        customer = self.get_or_create_customer(phone)
        keyword = text.strip().lower().rstrip(".!")

        if keyword in OPT_OUT_KEYWORDS:
            customer.opted_out = True
            learning.mark_opted_out(self.db, customer.id, self.settings, now)
            conv = cadence.open_conversation(self.db, customer.id, self.settings, now)
            reply = f"You're unsubscribed from {self.settings.restaurant_name} texts. Reply START to resubscribe."
            if conv:
                self._record(conv, "customer", "text", text, provider_message_id)
                self._set_state(conv, State.OPTED_OUT)
                self._record(conv, "agent", "text", reply)
            self.deps.messenger.send(customer.phone_number, reply)
            return TurnOutcome(reply=reply, state=State.OPTED_OUT, conversation_id=conv.id if conv else None)

        if keyword in OPT_IN_KEYWORDS and customer.opted_out:
            customer.opted_out = False
            reply = f"You're back on {self.settings.restaurant_name} texts. Hungry? Just text me what you want."
            self.deps.messenger.send(customer.phone_number, reply)
            return TurnOutcome(reply=reply, state=None)

        conv = self._active_conversation(customer, "text")
        self._record(conv, "customer", "text", text, provider_message_id)
        learning.mark_replied(self.db, conv, now)
        self.db.flush()

        if conv.human_takeover:
            conv.needs_attention = True
            return TurnOutcome(reply=None, state=State(conv.state), conversation_id=conv.id)

        reply, order = self._run_text_turn(conv, customer, text)
        self._record(conv, "agent", "text", reply)
        try:
            self.send(customer.phone_number, reply)
        except MessagingError as exc:
            log.error("reply to customer %s failed: %s", customer.id, exc)
        return TurnOutcome(
            reply=reply, state=State(conv.state), conversation_id=conv.id, order_id=order.id if order else None
        )

    def _history(self, conv: Conversation) -> list[dict]:
        msgs = [m for m in conv.messages if m.role in ("customer", "agent", "staff")][-HISTORY_LIMIT:]
        return [{"role": "user" if m.role == "customer" else "assistant", "content": m.content} for m in msgs]

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
            if not validated.errors or turn.intent in (Intent.DECLINE, Intent.HANDOFF):
                break
            tctx.corrections = validated.errors
        return turn, validated

    def _addon_for(self, conv: Conversation, customer: Customer) -> str | None:
        if State(conv.state) not in (State.AWAITING_RESPONSE, State.NEGOTIATING_MODIFICATIONS) or not conv.cart_json:
            return None
        favorites = customer.cadence.favorite_items if customer.cadence else []
        addon = learning.suggest_addon(self.db, [i["item_id"] for i in conv.cart_json], favorites)
        if not addon:
            return None
        name = self.menu.items[addon].name.split(" (")[0].lower()
        already = any(m.role == "agent" and name in m.content.lower() for m in conv.messages)
        return None if already else addon

    def resolve_fulfillment(
        self, conv: Conversation, customer: Customer, validated: ValidatedCart,
        requested: str | None, requested_address: str | None,
    ) -> FulfillmentDecision:
        fulfillment = requested or conv.fulfillment or "pickup"
        address = requested_address or conv.delivery_address or customer.address
        if fulfillment != "delivery":
            return FulfillmentDecision("pickup", address)

        delivery = self.deps.delivery
        if delivery is None:
            links = marketplace_links(self.settings)
            note = None
            if requested == "delivery":
                if links:
                    note = "For delivery you can grab it on our " + " or ".join(f"{n}: {u}" for n, u in links)
                else:
                    note = "We're pickup only right now, sorry! I've got it as pickup."
            return FulfillmentDecision("pickup", address, note)
        if not self.deps.payments.has_saved_card(customer) and not self.deps.payments.supports_links:
            note = "Delivery needs a card on file, so I've got this as pickup for now." if requested == "delivery" else None
            return FulfillmentDecision("pickup", address, note)
        minimum = round(self.settings.delivery_min_order * 100)
        if validated.lines and validated.subtotal_cents < minimum:
            return FulfillmentDecision(
                "delivery", address, f"Delivery minimum is {money(minimum)}. Want to add something, or do pickup?", True
            )
        if not address:
            return FulfillmentDecision("delivery", None, None, True)

        quote = conv.delivery_quote or {}
        fresh = (
            quote.get("address") == address
            and quote.get("at")
            and self.clock() - datetime.fromisoformat(quote["at"]) < QUOTE_TTL
        )
        if not fresh and validated.lines:
            try:
                q = delivery.quote(
                    external_id=f"bruno-q-{conv.id}-{int(self.clock().timestamp())}",
                    address=address,
                    phone=customer.phone_number,
                    name=customer.name or "Customer",
                    order_value_cents=validated.total_cents,
                )
            except DeliveryError as exc:
                log.info("delivery quote failed: %s", exc)
                conv.delivery_quote = None
                return FulfillmentDecision(
                    "delivery", address, "Hmm, DoorDash can't deliver to that address. Want to do pickup instead?", True
                )
            quote = {
                "address": address,
                "fee_cents": q.fee_cents,
                "eta": q.dropoff_eta.isoformat() if q.dropoff_eta else None,
                "at": self.clock().isoformat(),
            }
            conv.delivery_quote = quote
        eta = display_local(datetime.fromisoformat(quote["eta"]), self.settings.restaurant_timezone) if quote.get("eta") else None
        return FulfillmentDecision("delivery", address, None, False, eta)

    def _run_text_turn(self, conv: Conversation, customer: Customer, text: str) -> tuple[str, Order | None]:
        self.db.refresh(conv)
        current = State(conv.state)
        ctx = self._context(customer, conv, self._addon_for(conv, customer))
        tctx = TurnContext(
            system_prompt=system_prompt(ctx, self.menu, self.settings, self.deps.payments.supports_links),
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
            call = self.settings.restaurant_phone or self.settings.voice_phone_number or "the store"
            return f"Sorry, my phone's acting up. Give us a call at {call} and we'll get you sorted!", None

        self.remember(customer, turn.remember)

        if turn.intent == Intent.HANDOFF:
            conv.human_takeover = True
            conv.needs_attention = True
            self.notify_owner(
                f"Bruno: {customer.name or customer.phone_number} ({customer.phone_number}) needs a person: \"{text[:200]}\""
            )
            return humanize.clean(turn.reply), None

        before = ctx.validated.items()
        after = validated.items()
        new_pickup = conv.pickup_time
        if turn.pickup_time:
            new_pickup = self._ready(turn.pickup_time)
        decision = self.resolve_fulfillment(conv, customer, validated, turn.fulfillment, turn.delivery_address)
        changed = (
            _cart_key(before) != _cart_key(after)
            or new_pickup != conv.pickup_time
            or decision.fulfillment != conv.fulfillment
            or (decision.fulfillment == "delivery" and decision.address != conv.delivery_address)
        )
        target = next_state(
            current, turn.intent, cart_ready=validated.ready and not decision.blocked, cart_changed=changed
        )
        pay_by_link = self.needs_pay_link(customer, decision.fulfillment)
        if target == State.POS_INJECTION and pay_by_link:
            target = State.AWAITING_PAYMENT

        if target != State.DECLINED:
            conv.cart_json = [i.model_dump() for i in after]
            if after and not new_pickup:
                new_pickup = self._ready(None)
            conv.pickup_time = new_pickup
            conv.fulfillment = decision.fulfillment
            conv.delivery_address = decision.address if decision.fulfillment == "delivery" else conv.delivery_address
        self._set_state(conv, target)

        facts = recap_facts(
            ctx, self.menu, self.settings, after, conv.pickup_time, decision.fulfillment, decision.address, decision.eta,
            pay_by_link,
        )
        reply = humanize.clean(humanize.fill(turn.reply, facts))
        totals = order_totals(validated, decision.fulfillment, self.settings)
        allowed = {money(totals.total_cents), money(totals.delivery_fee_cents), money(totals.subtotal_cents)}
        if humanize.has_unverified_price(reply, allowed):
            log.warning("LLM wrote an unverified price; replacing reply")
            reply = ""

        if target == State.POS_INJECTION:
            result = self.finalize_order(conv, "text")
            return "\n\n".join(p for p in (reply, result.message) if p), result.order

        if target == State.AWAITING_PAYMENT:
            if current != State.AWAITING_PAYMENT or not conv.payment_link:
                return "\n\n".join(p for p in (reply, self.send_pay_link(conv, customer)) if p), None
            if conv.payment_link not in reply:
                reply = f"{reply}\n\nHere's that pay link again: {conv.payment_link}".strip()
            return reply, None

        if target == State.CONFIRMING_PAYMENT:
            if changed or current != State.CONFIRMING_PAYMENT:
                if facts["total"] not in reply:
                    recap = humanize.confirmation(facts, decision.fulfillment == "delivery", self.deps.rng)
                    reply = f"{reply}\n\n{recap}" if reply else recap
            elif not re.search(r"\b(yes|put it in|kitchen|good to go|fire)\b", reply, re.I):
                reply = f"{reply} {self.deps.rng.choice(humanize.READY_NUDGE)}".strip()
        elif validated.errors and "?" not in reply:
            reply = f"{reply} Quick q: {validated.errors[0]}".strip()
        elif decision.blocked and decision.fulfillment == "delivery" and not decision.address and "?" not in reply:
            reply = f"{reply} What's the address for delivery?".strip()
        if decision.note and decision.note not in reply and not (
            "pickup only" in decision.note and "pickup" in reply.lower()
        ):
            reply = f"{reply}\n\n{decision.note}" if reply else decision.note
        return reply or "Got it.", None

    # ------------------------------------------------------------------ payment links
    def needs_pay_link(self, customer: Customer, fulfillment: str) -> bool:
        payments = self.deps.payments
        if not payments.supports_links or payments.has_saved_card(customer):
            return False
        return fulfillment == "delivery" or self.settings.require_prepay

    def send_pay_link(self, conv: Conversation, customer: Customer) -> str:
        """Create a Stripe Checkout link for the current cart (state must be AWAITING_PAYMENT)."""
        validated = self.menu.validate(conv.cart_json)
        totals = order_totals(validated, conv.fulfillment, self.settings)
        try:
            link = self.deps.payments.payment_link(
                customer, totals.total_cents,
                f"{self.settings.restaurant_name}: {self.menu.describe(conv.cart_json)}", conv.id,
            )
        except PaymentError as exc:
            log.error("payment link failed for conversation %s: %s", conv.id, exc)
            self._set_state(conv, State.CONFIRMING_PAYMENT)
            if conv.fulfillment == "delivery":
                return "Hmm, my payment link isn't working right now. Want to do pickup and pay at the counter instead?"
            conv.payment_link = None
            return "Hmm, my payment link isn't working right now. You can just pay when you pick up. Want me to put it in?"
        conv.payment_link = link.url
        return f"Here's a secure link to pay ({money(totals.total_cents)}): {link.url}\n\nIt goes to the kitchen the second that's done."

    def payment_received(self, conv: Conversation, payment_reference: str, saved_card=None) -> FinalizeResult | None:
        """Stripe confirmed payment for a pay link: save the card, place the order, tell the customer."""
        customer = self.db.get(Customer, conv.customer_id)
        if saved_card:
            customer.payment_token, customer.card_label = saved_card.token, saved_card.label
        if State(conv.state) != State.AWAITING_PAYMENT:
            if State(conv.state) in (State.COMPLETED, State.POS_INJECTION):
                return None  # duplicate webhook
            self.deps.payments.refund(payment_reference)
            self.notify_owner(f"Bruno: refunded a late payment from {customer.name or customer.phone_number} (order was {conv.state}).")
            return None
        self._set_state(conv, State.POS_INJECTION)
        prepaid = PaymentResult(status="captured", reference=payment_reference, method=customer.card_label or "card")
        result = self.finalize_order(conv, conv.last_channel or "text", prepaid=prepaid)
        self._record(conv, "agent", "text", result.message)
        try:
            self.send(customer.phone_number, result.message)
        except MessagingError as exc:
            log.error("payment confirmation text failed: %s", exc)
        return result

    # ------------------------------------------------------------------ fulfillment
    def finalize_order(self, conv: Conversation, channel: str, prepaid: PaymentResult | None = None) -> FinalizeResult:
        """Charge, dispatch delivery (if any) and inject into the POS. Only from STATE_POS_INJECTION."""
        if State(conv.state) != State.POS_INJECTION:
            raise RuntimeError(f"finalize_order called in {conv.state}")
        customer = self.db.get(Customer, conv.customer_id)
        validated = self.menu.validate(conv.cart_json)
        call = self.settings.restaurant_phone or self.settings.voice_phone_number or "the store"
        if not validated.ready:
            self._set_state(conv, State.FAILED)
            return FinalizeResult(False, f"Something's off with that order. Mind calling us at {call}?")

        now = self.clock()
        tz = self.settings.restaurant_timezone
        pickup = self._ready(conv.pickup_time)
        conv.pickup_time = pickup
        local_now = to_local(now, tz)
        h, m = map(int, pickup.split(":"))
        pickup_at = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
        external_id = f"bruno-conv-{conv.id}"
        delivering = conv.fulfillment == "delivery" and self.deps.delivery is not None and conv.delivery_address
        totals = order_totals(validated, "delivery" if delivering else "pickup", self.settings)

        payment = prepaid or self.deps.payments.charge(customer, totals.total_cents, external_id)
        if not payment.ok:
            self._set_state(conv, State.FAILED)
            self._record(conv, "system", channel, f"Payment failed: {payment.error}")
            self.notify_owner(f"Bruno: card declined for {customer.name or customer.phone_number}")
            return FinalizeResult(False, f"Hmm, the card on file didn't go through. Give us a call at {call} and we'll sort it out!")

        dropoff = None
        if delivering:
            try:
                dropoff = self.deps.delivery.create(
                    external_id=external_id,
                    address=conv.delivery_address,
                    phone=customer.phone_number,
                    name=customer.name or "Customer",
                    order_value_cents=validated.total_cents,
                    pickup_time=pickup_at,
                    items_summary=self.menu.describe(conv.cart_json),
                )
            except DeliveryError as exc:
                log.error("DoorDash create failed for conversation %s: %s", conv.id, exc)
                if payment.reference:
                    self.deps.payments.refund(payment.reference)
                self._set_state(conv, State.FAILED)
                self._record(conv, "system", channel, f"Delivery dispatch failed: {exc}")
                return FinalizeResult(
                    False, "Ugh, couldn't get a DoorDash driver for that one and you weren't charged. Want to do pickup instead? Just text me again."
                )

        pos_order = POSOrder(
            external_id=external_id,
            restaurant=self.settings.restaurant_name,
            channel=channel,
            customer=POSCustomer(name=customer.name or f"Guest {customer.phone_number[-4:]}", phone=customer.phone_number),
            pickup_at=pickup_at,
            line_items=validated.lines,
            subtotal_cents=totals.subtotal_cents,
            tax_cents=totals.tax_cents,
            delivery_fee_cents=totals.delivery_fee_cents,
            total_cents=totals.total_cents,
            payment=POSPayment(status=payment.status, method=payment.method, reference=payment.reference),
            fulfillment="delivery" if delivering else "pickup",
            delivery_address=conv.delivery_address if delivering else None,
            delivery_id=dropoff.delivery_id if dropoff else None,
            customer_notes=list(customer.memory or [])[-3:],
            created_at=local_now,
        )
        try:
            result = self.deps.pos.inject(pos_order)
        except Exception as exc:
            log.exception("POS injection failed for conversation %s", conv.id)
            if payment.reference:
                self.deps.payments.refund(payment.reference)
            if dropoff:
                self.deps.delivery.cancel(dropoff.delivery_id)
            self._set_state(conv, State.FAILED)
            self._record(conv, "system", channel, f"POS injection failed: {exc}")
            self.notify_owner(f"Bruno: POS rejected an order for {customer.name or customer.phone_number}: {exc}")
            return FinalizeResult(
                False, f"Ugh, our system hiccuped putting that in and you weren't charged. Give us a call at {call} and we'll get it going!"
            )

        order = Order(
            customer_id=customer.id,
            items_json=conv.cart_json,
            total_amount=Decimal(totals.total_cents) / 100,
            order_date=now,
            channel=channel,
            source="bruno",
            status="completed",
            fulfillment="delivery" if delivering else "pickup",
            pickup_time=pickup,
            pos_order_id=result.pos_order_id,
            external_id=external_id,
            payment_reference=payment.reference,
            delivery_id=dropoff.delivery_id if dropoff else None,
            delivery_tracking_url=dropoff.tracking_url if dropoff else None,
            conversation_id=conv.id,
        )
        self.db.add(order)
        self._set_state(conv, State.COMPLETED)
        if delivering:
            customer.address = conv.delivery_address
        self.db.flush()
        learning.attribute_order(self.db, order, self.settings)
        cadence.recompute_customer(self.db, customer, self.settings, now)

        name = f", {customer.first_name}" if customer.name else ""
        total = money(totals.total_cents)
        if payment.status == "captured":
            paid = f"{total} on your {payment.method}."
        else:
            paid = f"It's {total}, pay when you grab it."
        if dropoff:
            eta = display_local(dropoff.dropoff_eta, tz)
            msg = f"You're all set{name}! A DoorDash driver will bring it by{' around ' + eta if eta else ''}. {paid}"
            if dropoff.tracking_url:
                msg += f"\n\nTrack it here: {dropoff.tracking_url}"
        else:
            msg = self.deps.rng.choice(
                [
                    f"You're all set{name}! Ready around {display_time(pickup)}. {paid} See you soon 🍕",
                    f"Done{name}, it's in. Ready ~{display_time(pickup)}. {paid}",
                    f"In the oven soon{name}! Ready around {display_time(pickup)}. {paid} See you in a bit.",
                ]
            )
        return FinalizeResult(True, msg, order)

    # ------------------------------------------------------------------ staff (dashboard)
    def staff_reply(self, conv: Conversation, text: str) -> None:
        customer = self.db.get(Customer, conv.customer_id)
        conv.human_takeover = True
        conv.needs_attention = False
        conv.updated_at = self.clock()
        self._record(conv, "staff", "text", text)
        self.deps.messenger.send(customer.phone_number, text)

    # ------------------------------------------------------------------ voice tools
    def voice_context(self, phone: str) -> CustomerContext:
        customer = self.get_or_create_customer(phone)
        conv = cadence.open_conversation(self.db, customer.id, self.settings, self.clock())
        return self._context(customer, conv)

    def voice_update_cart(
        self, phone: str, items: list[dict], pickup_time: str | None,
        fulfillment: str | None = None, address: str | None = None,
    ) -> dict:
        customer = self.get_or_create_customer(phone)
        conv = self._active_conversation(customer, "voice")
        validated = self.menu.validate(items)
        current = State(conv.state)
        if pickup_time:
            conv.pickup_time = self._ready(pickup_time)
        elif not conv.pickup_time:
            conv.pickup_time = self._ready(None)
        decision = self.resolve_fulfillment(conv, customer, validated, fulfillment, address)
        conv.cart_json = [i.model_dump() for i in validated.items()]
        conv.fulfillment = decision.fulfillment
        if decision.fulfillment == "delivery":
            conv.delivery_address = decision.address
        ready = validated.ready and not decision.blocked
        target = next_state(current, Intent.UPDATE_ORDER, cart_ready=ready, cart_changed=True)
        self._set_state(conv, target)
        label = self._context(customer, conv).payment_label
        totals = order_totals(validated, decision.fulfillment, self.settings)
        self._record(conv, "system", "voice", f"Voice cart update: {self.menu.describe(conv.cart_json) or 'empty'}")
        problems = list(validated.errors) + ([decision.note] if decision.note else [])
        if decision.blocked and decision.fulfillment == "delivery" and not decision.address:
            problems.append("Need the delivery address.")
        return {
            "ok": ready,
            "errors": problems,
            "summary": order_summary(validated, conv.pickup_time, label, totals.delivery_fee_cents) if validated.lines else "",
            "total": money(totals.total_cents),
            "fulfillment": decision.fulfillment,
            "ready_time": decision.eta or display_time(conv.pickup_time),
            "payment_method": label,
            "next_step": (
                "Read the items, total and time back to the caller and ask them to confirm. "
                "Only call place_order after they clearly say yes."
                if ready
                else "Sort out the problems listed in errors with the caller, then call update_cart again."
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
        if self.needs_pay_link(customer, conv.fulfillment):
            self._set_state(conv, State.AWAITING_PAYMENT)
            message = self.send_pay_link(conv, customer)
            if State(conv.state) == State.AWAITING_PAYMENT:
                self.send(customer.phone_number, message)
                self._record(conv, "agent", "text", message)
                return {"ok": True, "message": "I just texted you a secure payment link. The order goes in as soon as it's paid."}
            return {"ok": False, "message": message}
        self._set_state(conv, State.POS_INJECTION)
        result = self.finalize_order(conv, "voice")
        self._record(conv, "system", "voice", f"Voice place_order: {result.message}")
        return {"ok": result.ok, "message": result.message, "order_id": result.order.id if result.order else None}

    # ------------------------------------------------------------------ convenience
    def propose_pickup(self, customer: Customer) -> str:
        return propose_pickup(customer.cadence, self.clock(), self.settings)
