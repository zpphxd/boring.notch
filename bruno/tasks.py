"""Celery workers + beat schedule.

  celery -A bruno.tasks worker --loglevel=info
  celery -A bruno.tasks beat   --loglevel=info

Nightly: recompute every customer's cadence. 3:00 PM local: pick today's
drifting customers and queue their texts for the off-peak send time (4:30 PM).
"""

import logging
from datetime import UTC, datetime

from celery import Celery
from celery.schedules import crontab

from bruno import cadence
from bruno.agent import Bruno
from bruno.config import get_settings
from bruno.db import init_db, session_scope
from bruno.deps import get_deps
from bruno.locks import customer_lock
from bruno.models import Customer
from bruno.timeutil import local_to_utc, parse_hhmm, to_local, utcnow

log = logging.getLogger(__name__)
settings = get_settings()

celery_app = Celery("bruno", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.timezone = settings.restaurant_timezone
celery_app.conf.beat_schedule = {
    "nightly-cadence-sync": {
        "task": "bruno.tasks.recompute_cadences",
        "schedule": crontab(hour=3, minute=0),
    },
    "daily-outreach-scan": {
        "task": "bruno.tasks.schedule_outreach",
        "schedule": crontab(hour=settings.outreach_scan_hour, minute=0),
    },
}


def send_time_utc(now: datetime) -> datetime:
    """Today's configured off-peak send time, as naive UTC (or now if it's passed)."""
    h, m = parse_hhmm(settings.outreach_send_time) or (16, 30)
    local = to_local(now, settings.restaurant_timezone).replace(hour=h, minute=m, second=0, microsecond=0)
    return max(local_to_utc(local), now)


@celery_app.task(name="bruno.tasks.recompute_cadences")
def recompute_cadences() -> int:
    init_db()
    with session_scope() as session:
        return cadence.recompute_all(session, settings)


@celery_app.task(name="bruno.tasks.schedule_outreach")
def schedule_outreach() -> list[int]:
    init_db()
    now = utcnow()
    with session_scope() as session:
        cadence.recompute_all(session, settings, now)
        ids = [c.id for c in cadence.outreach_candidates(session, settings, now)]
    eta = send_time_utc(now)
    for customer_id in ids:
        send_outreach.apply_async(args=[customer_id], eta=eta.replace(tzinfo=UTC))
    log.info("queued %d outreach texts for %s UTC", len(ids), eta)
    return ids


@celery_app.task(name="bruno.tasks.send_outreach", autoretry_for=(ConnectionError,), max_retries=3)
def send_outreach(customer_id: int) -> int | None:
    with session_scope() as session:
        customer = session.get(Customer, customer_id)
        if not customer:
            return None
        with customer_lock(customer.phone_number):
            # Re-check eligibility at send time: they may have ordered or opted out since 3 PM.
            eligible = {c.id for c in cadence.outreach_candidates(session, settings)}
            if customer_id not in eligible:
                return None
            conv = Bruno(session, get_deps()).start_outreach(customer)
            return conv.id if conv else None
