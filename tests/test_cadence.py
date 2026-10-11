from datetime import datetime, timedelta

from bruno import cadence
from bruno import outreach
from bruno.models import Order
from tests.conftest import FRIDAY_430PM, PEPPERONI, make_customer


def test_average_gap():
    base = datetime(2026, 1, 1)
    assert cadence.average_gap_days([base, base + timedelta(days=7), base + timedelta(days=14)]) == 7
    assert cadence.average_gap_days([base]) is None


def test_drift_flag(settings):
    now = FRIDAY_430PM
    assert cadence.is_drifting(7, now - timedelta(days=21), now, settings)
    assert not cadence.is_drifting(7, now - timedelta(days=8), now, settings)  # within 1.25x
    assert not cadence.is_drifting(2, now - timedelta(days=4), now, settings)  # under min_drift_days


def test_usual_order_and_preferences(session, settings):
    c = make_customer(session, settings, now=FRIDAY_430PM)
    t = c.cadence
    assert t.drift_threshold_flag
    assert round(t.avg_days_between_orders) == 7
    assert t.usual_order_json[0]["item_id"] == "pizza"
    assert t.usual_order_json[0]["modifiers"] == ["pepperoni"]
    assert t.typical_pickup_time == "18:00"
    assert t.preferred_weekday == 4  # Friday


def test_candidates_respect_opt_out_cooldown_and_open_chats(session, settings, deps):
    now = FRIDAY_430PM
    dave = make_customer(session, settings, now=now)
    regular = make_customer(session, settings, now=now, phone="+15551230002", name="Sam", last_days_ago=2)
    opted = make_customer(session, settings, now=now, phone="+15551230003", name="Opt Out")
    opted.opted_out = True
    session.commit()

    ids = {c.id for c in cadence.outreach_candidates(session, settings, now)}
    assert ids == {dave.id}
    assert regular.id not in ids

    outreach.reach_out(session, deps, dave, now, mode="auto")
    session.commit()
    # Open conversation + cooldown: not picked again tomorrow
    assert cadence.outreach_candidates(session, settings, now + timedelta(days=1)) == []
    # A week later, still no reorder: wait a full cycle since the last nudge
    assert cadence.outreach_candidates(session, settings, now + timedelta(days=5)) == []
    assert [c.id for c in cadence.outreach_candidates(session, settings, now + timedelta(days=8))] == [dave.id]


def test_usual_prefers_most_frequent(session, settings):
    wings = [{"item_id": "wings", "size": None, "quantity": 1, "modifiers": ["bbq_sauce"], "notes": None}]
    c = make_customer(session, settings, now=FRIDAY_430PM, cart=PEPPERONI, count=3)

    for n in range(2):
        session.add(
            Order(
                customer_id=c.id, items_json=wings, total_amount=15, order_date=FRIDAY_430PM - timedelta(days=1 + n),
                channel="pos", status="completed",
            )
        )
    session.flush()
    t = cadence.recompute_customer(session, c, settings, FRIDAY_430PM)
    assert t.usual_order_json[0]["item_id"] == "pizza"


def test_segments(settings):
    now = FRIDAY_430PM
    d = lambda days: now - timedelta(days=days)  # noqa: E731
    assert cadence.segment_for(0, None, None, now, False) == "new"
    assert cadence.segment_for(1, None, d(5), now, False) == "new"
    assert cadence.segment_for(1, None, d(40), now, False) == "one_time"
    assert cadence.segment_for(1, None, d(400), now, False) == "dormant"
    assert cadence.segment_for(6, 7, d(3), now, False) == "regular"
    assert cadence.segment_for(6, 7, d(20), now, True) == "drifting"
    assert cadence.segment_for(6, 7, d(90), now, True) == "lapsed"


def test_profile_learns_value_and_favorites(session, settings):
    c = make_customer(session, settings, now=FRIDAY_430PM, count=4)
    assert float(c.lifetime_value) == round(21.06 * 4, 2)
    assert c.cadence.favorite_items == ["pizza"]
    assert c.cadence.next_expected_date is not None


def test_config_overrides_from_dashboard(session_factory):
    from bruno import configstore
    from bruno.config import Settings, merge

    with session_factory() as s:
        configstore.set_values(s, {"openai_api_key": "sk-test", "daily_outreach_cap": "40", "autopilot_enabled": "true"})
        s.commit()
        merged = merge(Settings(_env_file=None), configstore.read_overrides(s))
    assert merged.openai_api_key == "sk-test" and merged.daily_outreach_cap == 40 and merged.autopilot_enabled is True
