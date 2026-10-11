"""Create the tables and 3 test customers with order history.

  python scripts/seed.py --phone +15551234567 --reset

Your number becomes "Dave", a Friday-night large-pepperoni regular who has
gone quiet for three weeks (so he gets picked for outreach). Pass --phone up
to three times to put real numbers on the other two test customers.
"""

import _bootstrap  # noqa: F401
import argparse
from datetime import timedelta
from decimal import Decimal

from bruno import cadence
from bruno.config import get_settings
from bruno.db import Base, get_engine, init_db, session_scope
from bruno.menu import get_menu
from bruno.messaging import normalize_phone
from bruno.models import Customer, Order
from bruno.timeutil import utcnow

PROFILES = [
    {
        "name": "Dave Rossi",
        "fallback_phone": "+15555550101",
        "every_days": 7,
        "last_days_ago": 21,
        "history": 8,
        "cart": [{"item_id": "pizza", "size": "large", "quantity": 1, "modifiers": ["pepperoni"]}],
        "pickup": "18:00",
        "payment_token": "tok_demo_visa",
        "card_label": "Visa •4242",
    },
    {
        "name": "Maria Lopez",
        "fallback_phone": "+15555550102",
        "every_days": 10,
        "last_days_ago": 26,
        "history": 6,
        "cart": [
            {"item_id": "wings", "quantity": 1, "modifiers": ["buffalo_sauce"]},
            {"item_id": "garlic_knots", "quantity": 1},
        ],
        "pickup": "19:00",
        "payment_token": None,
        "card_label": None,
    },
    {
        "name": "Sam Patel",
        "fallback_phone": "+15555550103",
        "every_days": 7,
        "last_days_ago": 3,
        "history": 10,
        "cart": [
            {"item_id": "margherita", "size": "medium", "quantity": 1},
            {"item_id": "coke", "quantity": 2},
        ],
        "pickup": "17:30",
        "payment_token": "tok_demo_amex",
        "card_label": "Amex •1005",
    },
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phone", action="append", default=[], help="Your cell (E.164). Repeat up to 3x.")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate all tables first.")
    args = parser.parse_args()

    settings = get_settings()
    menu = get_menu()
    if args.reset:
        from bruno import models  # noqa: F401

        Base.metadata.drop_all(get_engine())
    init_db()

    now = utcnow()
    with session_scope() as session:
        for idx, profile in enumerate(PROFILES):
            phone = normalize_phone(args.phone[idx]) if idx < len(args.phone) else profile["fallback_phone"]
            customer = session.query(Customer).filter_by(phone_number=phone).first()
            if customer:
                print(f"skip: {phone} already exists (use --reset)")
                continue
            customer = Customer(
                name=profile["name"],
                phone_number=phone,
                created_at=now - timedelta(days=200),
                payment_token=profile["payment_token"],
                card_label=profile["card_label"],
            )
            session.add(customer)
            session.flush()
            total = Decimal(menu.validate(profile["cart"]).total_cents) / 100
            for n in range(profile["history"]):
                when = now - timedelta(days=profile["last_days_ago"] + n * profile["every_days"])
                session.add(
                    Order(
                        customer_id=customer.id,
                        items_json=profile["cart"],
                        total_amount=total,
                        order_date=when,
                        channel="pos",
                        status="completed",
                        pickup_time=profile["pickup"],
                        external_id=f"seed-{phone}-{n}",
                    )
                )
            session.flush()
            t = cadence.recompute_customer(session, customer, settings, now)
            print(
                f"{customer.name:<12} {phone:<14} every ~{t.avg_days_between_orders:.0f}d, "
                f"last {profile['last_days_ago']}d ago, usual: {menu.describe(t.usual_order_json)}"
                f"{'  <- DRIFTING' if t.drift_threshold_flag else ''}"
            )
    print(f"\nSeeded database: {settings.database_url}")


if __name__ == "__main__":
    main()
