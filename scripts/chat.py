"""Talk to Bruno from your terminal as if you were the customer texting in.

  python scripts/chat.py --phone +15555550101 --outreach

Runs the exact same engine as the /webhook/incoming endpoint, with texts and
kitchen tickets printed locally. Commands:
  /call   show what the voice agent would greet you with right now
  /state  show the conversation state and cart
  /quit
"""

import _bootstrap  # noqa: F401
import argparse
import json
from dataclasses import replace

from bruno import cadence
from bruno.agent import Bruno
from bruno.db import init_db, session_scope
from bruno.deps import get_deps
from bruno.messaging import ConsoleMessenger, normalize_phone
from bruno.models import Customer
from bruno.pos import ConsolePOS
from bruno.voice import dynamic_variables


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phone", required=True)
    parser.add_argument("--outreach", action="store_true", help="Start with Bruno's outbound text.")
    args = parser.parse_args()

    init_db()
    base = get_deps()
    messenger = ConsoleMessenger(echo=False)
    deps = replace(base, messenger=messenger, pos=ConsolePOS(base.settings.kitchen_ticket_log))
    phone = normalize_phone(args.phone)
    print(f"LLM: {getattr(deps.llm, 'name', deps.llm)} | chatting as {phone}\n")

    if args.outreach:
        with session_scope() as session:
            customer = session.query(Customer).filter_by(phone_number=phone).first()
            if not customer:
                raise SystemExit("Unknown number; run scripts/seed.py first or drop --outreach.")
            if not Bruno(session, deps).start_outreach(customer):
                raise SystemExit("Could not start outreach (opted out or no usual order).")
        print(f"bruno> {messenger.sent[-1][1]}\n")

    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text == "/quit":
            break
        with session_scope() as session:
            agent = Bruno(session, deps)
            if text == "/call":
                ctx = agent.voice_context(phone)
                print(f"📞 voice> {dynamic_variables(ctx, deps.settings, deps.menu)['greeting']}\n")
                continue
            if text == "/state":
                customer = agent.get_or_create_customer(phone)
                conv = cadence.open_conversation(session, customer.id, deps.settings)
                if conv:
                    print(f"[{conv.state}] pickup={conv.pickup_time} cart={json.dumps(conv.cart_json)}\n")
                else:
                    print("[no open conversation]\n")
                continue
            outcome = agent.handle_inbound_text(phone, text)
        if outcome.reply:
            print(f"bruno> {outcome.reply}\n")


if __name__ == "__main__":
    main()
