"""The feedback loop: which outreach approaches actually turn into orders.

Every proactive text is an `Outreach` row tagged with a strategy and tone.
When the customer orders within the attribution window it counts as a win.
Bruno picks the approach for each new text with Thompson sampling, so it
keeps exploring a little and shifts toward whatever converts for this
restaurant's customers.
"""

import random
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bruno.config import Settings
from bruno.models import Conversation, Order, Outreach

STRATEGIES_BY_SEGMENT = {
    "drifting": ["usual"],
    "lapsed": ["win_back", "usual"],
    "one_time": ["second_order"],
}
TONES = ["casual", "playful", "brief"]
OPT_OUT_PENALTY = 3  # an opt-out is much worse than a non-reply


@dataclass
class ArmStats:
    arm: str
    sent: int = 0
    wins: int = 0
    losses: int = 0
    pending: int = 0
    replies: int = 0
    opt_outs: int = 0
    revenue: float = 0.0

    @property
    def conversion(self) -> float:
        decided = self.wins + self.losses
        return self.wins / decided if decided else 0.0


def arm_stats(session: Session, settings: Settings, now: datetime) -> dict[str, ArmStats]:
    window = timedelta(hours=settings.attribution_hours)
    stats: dict[str, ArmStats] = {}
    for o in session.scalars(select(Outreach).where(Outreach.status == "sent")):
        s = stats.setdefault(o.arm, ArmStats(o.arm))
        s.sent += 1
        s.replies += o.replied_at is not None
        if o.converted_order_id:
            s.wins += 1
            s.revenue += float(o.revenue or 0)
        elif o.opted_out:
            s.losses += OPT_OUT_PENALTY
            s.opt_outs += 1
        elif o.sent_at and now - o.sent_at > window:
            s.losses += 1
        else:
            s.pending += 1
    return stats


def choose_approach(
    session: Session, segment: str, settings: Settings, now: datetime, rng: random.Random | None = None
) -> tuple[str, str]:
    rng = rng or random.Random()
    stats = arm_stats(session, settings, now)
    best, best_score = None, -1.0
    for strategy in STRATEGIES_BY_SEGMENT.get(segment, ["usual"]):
        for tone in TONES:
            s = stats.get(f"{strategy}:{tone}", ArmStats(""))
            score = rng.betavariate(s.wins + 1, s.losses + 1)
            if score > best_score:
                best, best_score = (strategy, tone), score
    return best


def attribute_order(session: Session, order: Order, settings: Settings) -> Outreach | None:
    """Credit the most recent outreach if this order landed within the window."""
    window = timedelta(hours=settings.attribution_hours)
    outreach = session.scalars(
        select(Outreach)
        .where(
            Outreach.customer_id == order.customer_id,
            Outreach.status == "sent",
            Outreach.converted_order_id.is_(None),
            Outreach.sent_at <= order.order_date,
            Outreach.sent_at >= order.order_date - window,
        )
        .order_by(Outreach.sent_at.desc())
        .limit(1)
    ).first()
    if outreach:
        outreach.converted_order_id = order.id
        outreach.revenue = order.total_amount
    return outreach


def mark_replied(session: Session, conv: Conversation, now: datetime) -> None:
    if conv.outreach_id:
        outreach = session.get(Outreach, conv.outreach_id)
        if outreach and outreach.replied_at is None:
            outreach.replied_at = now


def mark_opted_out(session: Session, customer_id: int, settings: Settings, now: datetime) -> None:
    outreach = session.scalars(
        select(Outreach)
        .where(Outreach.customer_id == customer_id, Outreach.status == "sent")
        .order_by(Outreach.sent_at.desc())
        .limit(1)
    ).first()
    if outreach and outreach.sent_at and now - outreach.sent_at < timedelta(days=3):
        outreach.opted_out = True


def summary(session: Session, settings: Settings, now: datetime, days: int = 30) -> dict:
    since = now - timedelta(days=days)
    sent = list(session.scalars(select(Outreach).where(Outreach.status == "sent", Outreach.sent_at >= since)))
    wins = [o for o in sent if o.converted_order_id]
    bruno_orders = session.scalar(
        select(func.count(Order.id)).where(Order.order_date >= since, Order.channel.in_(("text", "voice")))
    )
    bruno_revenue = session.scalar(
        select(func.coalesce(func.sum(Order.total_amount), 0)).where(
            Order.order_date >= since, Order.channel.in_(("text", "voice"))
        )
    )
    return {
        "days": days,
        "sent": len(sent),
        "replies": sum(o.replied_at is not None for o in sent),
        "conversions": len(wins),
        "attributed_revenue": float(sum((o.revenue or Decimal(0)) for o in wins)),
        "opt_outs": sum(o.opted_out for o in sent),
        "reply_rate": (sum(o.replied_at is not None for o in sent) / len(sent)) if sent else 0.0,
        "conversion_rate": (len(wins) / len(sent)) if sent else 0.0,
        "bruno_orders": bruno_orders or 0,
        "bruno_revenue": float(bruno_revenue or 0),
        "pending_approval": session.scalar(
            select(func.count(Outreach.id)).where(Outreach.status == "pending_approval")
        ) or 0,
    }


# --------------------------------------------------------------------------- add-on suggestions
_pair_cache: dict[int, tuple[float, int, dict[str, Counter]]] = {}


def pairings(session: Session) -> dict[str, Counter]:
    """item -> Counter(other items ordered in the same order), company-wide."""
    key = id(session.get_bind())
    count = session.scalar(select(func.count(Order.id))) or 0
    cached = _pair_cache.get(key)
    if cached and cached[1] == count and time.time() - cached[0] < 600:
        return cached[2]
    pairs: dict[str, Counter] = defaultdict(Counter)
    for (items,) in session.execute(select(Order.items_json).order_by(Order.id.desc()).limit(20000)):
        ids = {i["item_id"] for i in items or []}
        for a in ids:
            for b in ids:
                if a != b:
                    pairs[a][b] += 1
    _pair_cache[key] = (time.time(), count, pairs)
    return pairs


def suggest_addon(session: Session, cart_item_ids: list[str], favorites: list[str] | None) -> str | None:
    if not cart_item_ids:
        return None
    pairs = pairings(session)
    scores: Counter[str] = Counter()
    for item in cart_item_ids:
        for other, n in pairs.get(item, {}).items():
            if other not in cart_item_ids and n >= 3:
                scores[other] += n
    for fav in favorites or []:
        if fav not in cart_item_ids and fav in scores:
            scores[fav] *= 2  # they've liked it before
    return scores.most_common(1)[0][0] if scores else None
