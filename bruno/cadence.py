"""State & cadence engine: learns each customer's ordering rhythm and flags drift."""

import json
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from bruno.config import Settings
from bruno.fsm import TERMINAL, State
from bruno.models import CadenceTracker, Conversation, Customer, Order
from bruno.timeutil import to_local, utcnow


def average_gap_days(dates: list[datetime]) -> float | None:
    dates = sorted(dates)
    if len(dates) < 2:
        return None
    gaps = [(b - a).total_seconds() / 86400 for a, b in zip(dates, dates[1:])]
    return sum(gaps) / len(gaps)


def is_drifting(avg_days: float | None, last: datetime | None, now: datetime, settings: Settings) -> bool:
    if avg_days is None or last is None:
        return False
    since = (now - last).total_seconds() / 86400
    return since >= settings.min_drift_days and since > avg_days * settings.drift_multiplier


def usual_order(orders: list[Order]) -> list[dict] | None:
    """Most frequently repeated cart (ties -> most recent)."""
    if not orders:
        return None

    def signature(items: list[dict]) -> str:
        norm = [
            {
                "item_id": i["item_id"],
                "size": i.get("size"),
                "quantity": i.get("quantity", 1),
                "modifiers": sorted(i.get("modifiers", [])),
            }
            for i in items
        ]
        return json.dumps(sorted(norm, key=lambda x: json.dumps(x, sort_keys=True)), sort_keys=True)

    ordered = sorted(orders, key=lambda o: o.order_date)
    counts = Counter(signature(o.items_json) for o in ordered)
    last_seen = {signature(o.items_json): idx for idx, o in enumerate(ordered)}
    best = max(counts, key=lambda s: (counts[s], last_seen[s]))
    return json.loads(best)


def recompute_customer(session: Session, customer: Customer, settings: Settings, now: datetime | None = None) -> CadenceTracker:
    now = now or utcnow()
    orders = list(
        session.scalars(
            select(Order)
            .where(Order.customer_id == customer.id, Order.status == "completed")
            .order_by(Order.order_date)
        )
    )
    tracker = session.get(CadenceTracker, customer.id) or CadenceTracker(customer_id=customer.id)
    dates = [o.order_date for o in orders]
    tracker.avg_days_between_orders = (
        average_gap_days(dates) if len(dates) >= settings.min_orders_for_cadence else None
    )
    tracker.last_order_date = dates[-1] if dates else None
    tracker.drift_threshold_flag = is_drifting(tracker.avg_days_between_orders, tracker.last_order_date, now, settings)
    tracker.usual_order_json = usual_order(orders)

    local = [to_local(d, settings.restaurant_timezone) for d in dates]
    if local:
        tracker.preferred_weekday = Counter(d.weekday() for d in local).most_common(1)[0][0]
        # Typical pickup: the most common half-hour slot they've ordered for.
        slots = Counter(
            (o.pickup_time or f"{ld.hour:02d}:{0 if ld.minute < 30 else 30:02d}") for o, ld in zip(orders, local)
        )
        tracker.typical_pickup_time = slots.most_common(1)[0][0]
    session.add(tracker)
    customer.total_orders = len(orders)
    return tracker


def recompute_all(session: Session, settings: Settings, now: datetime | None = None) -> int:
    customers = list(session.scalars(select(Customer)))
    for c in customers:
        recompute_customer(session, c, settings, now)
    return len(customers)


def open_conversation(session: Session, customer_id: int, settings: Settings, now: datetime | None = None) -> Conversation | None:
    now = now or utcnow()
    conv = session.scalars(
        select(Conversation)
        .where(Conversation.customer_id == customer_id)
        .order_by(Conversation.id.desc())
        .limit(1)
    ).first()
    if not conv or State(conv.state) in TERMINAL:
        return None
    if now - conv.updated_at > timedelta(hours=settings.conversation_ttl_hours):
        return None
    return conv


def outreach_candidates(session: Session, settings: Settings, now: datetime | None = None) -> list[Customer]:
    """Customers who are drifting and safe to contact right now."""
    now = now or utcnow()
    rows = session.execute(
        select(Customer, CadenceTracker)
        .join(CadenceTracker, CadenceTracker.customer_id == Customer.id)
        .where(CadenceTracker.drift_threshold_flag.is_(True), Customer.opted_out.is_(False))
    ).all()
    out = []
    for customer, tracker in rows:
        if not tracker.usual_order_json:
            continue
        if tracker.last_outreach_at and now - tracker.last_outreach_at < timedelta(days=settings.outreach_cooldown_days):
            continue
        if tracker.last_outreach_at and tracker.last_order_date and tracker.last_outreach_at > tracker.last_order_date:
            # Already nudged since their last order and they didn't bite: wait a full cycle.
            gap = timedelta(days=max(tracker.avg_days_between_orders or 0, settings.outreach_cooldown_days))
            if now - tracker.last_outreach_at < gap:
                continue
        if open_conversation(session, customer.id, settings, now):
            continue
        out.append(customer)
    return out
