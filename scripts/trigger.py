"""Fire outbound re-engagement texts.

  python scripts/trigger.py            # text every drifting customer (respects cooldowns)
  python scripts/trigger.py --all      # text every customer with a usual order (demo mode)
  python scripts/trigger.py --dry-run  # print the texts instead of sending them

Uses MESSAGING_PROVIDER from .env (console | sendblue).
"""

import _bootstrap  # noqa: F401
import argparse
from dataclasses import replace

from sqlalchemy import select

from bruno import cadence
from bruno.agent import Bruno
from bruno.db import init_db, session_scope
from bruno.deps import get_deps
from bruno.locks import customer_lock
from bruno.messaging import ConsoleMessenger
from bruno.models import Customer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="Ignore drift/cooldown checks.")
    parser.add_argument("--customer-id", type=int, help="Only this customer.")
    parser.add_argument("--dry-run", action="store_true", help="Print instead of sending (nothing is saved).")
    args = parser.parse_args()

    init_db()
    deps = get_deps()
    if args.dry_run:
        deps = replace(deps, messenger=ConsoleMessenger())

    with session_scope() as session:
        cadence.recompute_all(session, deps.settings)
        if args.all or args.customer_id:
            customers = list(session.scalars(select(Customer).where(Customer.opted_out.is_(False))))
            if args.customer_id:
                customers = [c for c in customers if c.id == args.customer_id]
        else:
            customers = cadence.outreach_candidates(session, deps.settings)

        if not customers:
            print("Nobody to contact right now (try --all for the demo).")
            return

        agent = Bruno(session, deps)
        for customer in customers:
            with customer_lock(customer.phone_number):
                conv = agent.start_outreach(customer)
            status = f"sent (conversation {conv.id})" if conv else "skipped"
            print(f"{customer.name or customer.phone_number}: {status}")

        if args.dry_run:
            session.rollback()
            print("(dry run: nothing saved)")


if __name__ == "__main__":
    main()
