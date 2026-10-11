"""Command line.

  python -m bruno                       run the app (dashboard + webhooks + autopilot)
  python -m bruno demo --phone +1...    load demo customers (your number = "Dave")
  python -m bruno chat --phone +1...    text with Bruno in your terminal
  python -m bruno import orders.csv --source square
  python -m bruno outreach --count 5 [--send]
  python -m bruno retell-config --base-url https://...
"""

import argparse
import json
import logging
import os
from dataclasses import replace


def serve(args: argparse.Namespace) -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        "bruno.api:app", host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*",
        log_level="info",
    )


def demo(args: argparse.Namespace) -> None:
    from bruno.config import get_settings
    from bruno.db import init_db, session_scope
    from bruno.demo import seed_demo
    from bruno.menu import get_menu

    init_db()
    with session_scope() as session:
        made = seed_demo(session, get_menu(), get_settings(), args.phone)
        for c in made[:8]:
            t = c.cadence
            print(f"  {c.name:<16} {c.phone_number:<14} {t.segment:<9} usual: {get_menu().describe(t.usual_order_json or [])}")
    print(f"Loaded {len(made)} demo customers.")


def chat(args: argparse.Namespace) -> None:
    from bruno import cadence, outreach
    from bruno.agent import Bruno
    from bruno.db import init_db, session_scope
    from bruno.deps import get_deps
    from bruno.messaging import ConsoleMessenger, normalize_phone
    from bruno.models import Customer
    from bruno.pos import ConsolePOS
    from bruno.timeutil import utcnow
    from bruno.voice import dynamic_variables

    init_db()
    base = get_deps()
    messenger = ConsoleMessenger(echo=False)
    deps = replace(
        base, messenger=messenger, pos=ConsolePOS(base.settings.kitchen_ticket_log),
        settings=base.settings.model_copy(update={"human_delays": False}),
    )
    phone = normalize_phone(args.phone)
    print(f"AI: {getattr(deps.llm, 'name', deps.llm)} · you are {phone} · /call /state /quit\n")

    def show(start: int) -> None:
        for _, text in messenger.sent[start:]:
            print(f"bruno> {text}\n")

    if args.outreach:
        with session_scope() as session:
            customer = session.query(Customer).filter_by(phone_number=phone).first()
            if not customer:
                raise SystemExit("Unknown number: run `python -m bruno demo --phone ...` first.")
            if not outreach.reach_out(session, deps, customer, utcnow(), mode="auto"):
                raise SystemExit("Can't text them (opted out or no order history).")
        show(0)

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
                print(f"📞 {dynamic_variables(agent.voice_context(phone), deps.settings, deps.menu)['greeting']}\n")
                continue
            if text == "/state":
                customer = agent.get_or_create_customer(phone)
                conv = cadence.open_conversation(session, customer.id, deps.settings)
                print(f"[{conv.state} · {conv.fulfillment}] {json.dumps(conv.cart_json)}\n" if conv else "[no open conversation]\n")
                continue
            start = len(messenger.sent)
            agent.handle_inbound_text(phone, text)
        show(start)


def do_import(args: argparse.Namespace) -> None:
    from bruno.config import get_settings
    from bruno.db import init_db, session_scope
    from bruno.importer import import_csv, unmatched_summary
    from bruno.menu import get_menu

    init_db()
    with open(args.file, "rb") as fh, session_scope() as session:
        r = import_csv(session, fh.read(), get_menu(), get_settings(), source=args.source, consented=not args.no_consent)
    print(f"{r.orders_created} orders, {r.customers_created} new customers, {r.orders_skipped_duplicate} duplicates, "
          f"{r.rows_without_phone} without phone. Columns: {r.columns}")
    for e in r.errors[:10]:
        print("  !", e)
    for name, n in unmatched_summary(r):
        print(f"  unmatched item: {name} ({n}x)")


def run_outreach(args: argparse.Namespace) -> None:
    from bruno import cadence, outreach
    from bruno.db import init_db, session_scope
    from bruno.deps import get_deps
    from bruno.timeutil import utcnow

    init_db()
    deps = get_deps()
    now = utcnow()
    with session_scope() as session:
        cadence.recompute_all(session, deps.settings, now)
        for customer in cadence.outreach_candidates(session, deps.settings, now)[: args.count]:
            o = outreach.reach_out(session, deps, customer, now, mode="auto" if args.send else "approve")
            if o:
                print(f"[{o.status}] {customer.name}: {o.message}\n")


def retell_config(args: argparse.Namespace) -> None:
    from bruno.config import get_settings
    from bruno.voice import RETELL_FUNCTIONS, VOICE_AGENT_PROMPT

    base = args.base_url.rstrip("/")
    secret = get_settings().webhook_secret
    qs = f"?secret={secret}" if secret else ""
    print(f"== Phone number -> Inbound call webhook URL ==\n{base}/voice/retell/inbound{qs}\n")
    print(f"== Agent prompt ==\n{VOICE_AGENT_PROMPT}")
    print("== Custom functions (keep 'args only' payloads OFF) ==")
    for fn in RETELL_FUNCTIONS:
        print(f"\n# {fn['name']}  ->  POST {base}/voice/retell/functions/{fn['name']}{qs}\n{fn['description']}")
        print(json.dumps(fn["parameters"], indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="bruno", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("serve")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    p = sub.add_parser("demo")
    p.add_argument("--phone", help="Your cell; becomes demo customer Dave")
    p = sub.add_parser("chat")
    p.add_argument("--phone", required=True)
    p.add_argument("--outreach", action="store_true", help="Start with Bruno texting you first")
    p = sub.add_parser("import")
    p.add_argument("file")
    p.add_argument("--source", default="pos", help="square | toast | clover | doordash | grubhub | pos")
    p.add_argument("--no-consent", action="store_true", help="Import for learning only; don't text these customers")
    p = sub.add_parser("outreach")
    p.add_argument("--count", type=int, default=5)
    p.add_argument("--send", action="store_true", help="Send now instead of drafting for approval")
    p = sub.add_parser("retell-config")
    p.add_argument("--base-url", required=True)

    args = parser.parse_args()
    handlers = {"demo": demo, "chat": chat, "import": do_import, "outreach": run_outreach, "retell-config": retell_config}
    if args.cmd in handlers:
        handlers[args.cmd](args)
    else:
        if args.cmd is None:
            args.host, args.port = "0.0.0.0", int(os.environ.get("PORT", 8000))
        serve(args)


if __name__ == "__main__":
    main()
