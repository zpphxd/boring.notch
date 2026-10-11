"""Learns each customer from their order history: rhythm, usual order,
favorites, typical time, value, and which segment they're in."""

import json
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from bruno.config import Settings
from bruno.fsm import TERMINAL, State
from bruno.models import CadenceTracker, Conversation, Customer, Order
from bruno.timeutil import to_local, utcnow

# Segments Bruno will proactively text. "regular" customers are on rhythm and
# don't need a nudge; "dormant" ones (a single order a long time ago) are cold.
CONTACTABLE = {"drifting", "lapsed", "one_time"}


def average_gap_days(dates: list[datetime]) -> float | None:
    dates = sorted(dates)
    if len(dates) < 2:
        return None
    gaps = [(b - a).total_seconds() / 86400 for a, b in zip(dates, dates[1:])]
    # Recent behaviour matters more: weight later gaps higher.
    weights = range(1, len(gaps) + 1)
    return sum(g * w for g, w in zip(gaps, weights)) / sum(weights)


def is_drifting(avg_days: float | None, last: datetime | None, now: datetime, settings: Settings) -> bool:
    if avg_days is None or last is None:
        return False
    since = (now - last).total_seconds() / 86400
    return since >= settings.min_drift_days and since > avg_days * settings.drift_multiplier


def segment_for(n_orders: int, avg: float | None, last: datetime | None, now: datetime, drifting: bool) -> str:
    if n_orders == 0 or last is None:
        return "new"
    since = (now - last).days
    if n_orders == 1:
        if since < 14:
            return "new"
        return "one_time" if since <= 150 else "dormant"
    if since > 365:
        return "dormant"
    if avg is not None and since > max(avg * 3, 60):
        return "lapsed"
    return "drifting" if drifting else "regular"


def _signature(items: list[dict]) -> str:
    norm = [
        {
            "item_id": i["item_id"],
            "size": i.get("size"),
            "quantity": i.get("quantity", 1),
            "modifiers": sorted(i.get("modifiers", [])),
            "notes": None,
        }
        for i in items
    ]
    return json.dumps(sorted(norm, key=lambda x: json.dumps(x, sort_keys=True)), sort_keys=True)


def usual_order(orders: list[Order]) -> list[dict] | None:
    """Most frequently repeated cart; ties go to the most recent. Only recent-ish
    history counts so a customer whose taste changed gets their new usual."""
    orders = [o for o in sorted(orders, key=lambda o: o.order_date) if o.items_json][-12:]
    if not orders:
        return None
    counts = Counter(_signature(o.items_json) for o in orders)
    last_seen = {_signature(o.items_json): idx for idx, o in enumerate(orders)}
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
    avg = average_gap_days(dates) if len(dates) >= settings.min_orders_for_cadence else None
    tracker.avg_days_between_orders = avg
    tracker.last_order_date = dates[-1] if dates else None
    tracker.drift_threshold_flag = is_drifting(avg, tracker.last_order_date, now, settings)
    tracker.segment = segment_for(len(orders), avg, tracker.last_order_date, now, tracker.drift_threshold_flag)
    tracker.usual_order_json = usual_order(orders)
    tracker.next_expected_date = tracker.last_order_date + timedelta(days=avg) if avg and tracker.last_order_date else None

    item_counts: Counter[str] = Counter()
    for o in orders:
        for i in o.items_json or []:
            item_counts[i["item_id"]] += i.get("quantity", 1)
    tracker.favorite_items = [i for i, _ in item_counts.most_common(3)]

    totals = [float(o.total_amount or 0) for o in orders]
    tracker.avg_ticket = sum(totals) / len(totals) if totals else None
    customer.lifetime_value = Decimal(str(round(sum(totals), 2)))

    local = [to_local(d, settings.restaurant_timezone) for d in dates]
    if local:
        recent = local[-10:]
        tracker.preferred_weekday = Counter(d.weekday() for d in recent).most_common(1)[0][0]
        slots = Counter(
            (o.pickup_time or f"{ld.hour:02d}:{0 if ld.minute < 30 else 30:02d}")
            for o, ld in zip(orders[-10:], recent)
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
    """Customers worth a proactive text right now, most valuable first."""
    now = now or utcnow()
    rows = session.execute(
        select(Customer, CadenceTracker)
        .join(CadenceTracker, CadenceTracker.customer_id == Customer.id)
        .where(CadenceTracker.segment.in_(CONTACTABLE), Customer.opted_out.is_(False))
    ).all()
    out = []
    for customer, tracker in rows:
        if not tracker.usual_order_json:
            continue
        if tracker.last_outreach_at and now - tracker.last_outreach_at < timedelta(days=settings.outreach_cooldown_days):
            continue
        if tracker.last_outreach_at and tracker.last_order_date and tracker.last_outreach_at > tracker.last_order_date:
            # Already nudged since their last order and they didn't bite: wait a full cycle
            # (and back off harder for lapsed customers).
            base = max(tracker.avg_days_between_orders or 0, settings.outreach_cooldown_days)
            if tracker.segment != "drifting":
                base = max(base, 21)
            if now - tracker.last_outreach_at < timedelta(days=base):
                continue
        if open_conversation(session, customer.id, settings, now):
            continue
        out.append(customer)

    def priority(c: Customer) -> float:
        t = c.cadence
        overdue = 1.0
        if t.avg_days_between_orders and t.last_order_date:
            overdue = min((now - t.last_order_date).days / t.avg_days_between_orders, 4.0)
        return float(t.avg_ticket or 15) * (1 + min(c.total_orders, 20) / 5) / overdue**0.5

    return sorted(out, key=priority, reverse=True)
