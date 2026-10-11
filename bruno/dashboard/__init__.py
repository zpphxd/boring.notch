"""Owner dashboard (server-rendered). Mounted at /admin."""

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from bruno import cadence, configstore, importer, learning, outreach
from bruno.agent import Bruno
from bruno.config import FIELDS
from bruno.deps import Deps
from bruno.delivery import marketplace_links
from bruno.fsm import TERMINAL, State
from bruno.llm import LLMError
from bruno.menu import Menu, money
from bruno.messaging import BlueBubblesMessenger, MessagingError, normalize_phone
from bruno.models import CadenceTracker, Conversation, Customer, Message, Order, Outreach
from bruno.orchestrator import order_summary, order_totals
from bruno.payments import payment_method_label
from bruno.timeutil import display_local, to_local, utcnow

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


def _ago(dt: datetime | None) -> str:
    if not dt:
        return "never"
    secs = (utcnow() - dt).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    days = int(secs // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


templates.env.filters["ago"] = _ago
templates.env.filters["money"] = lambda v: money(round(float(v or 0) * 100))
templates.env.filters["pct"] = lambda v: f"{(v or 0) * 100:.0f}%"
templates.env.filters["clock"] = lambda hhmm: (
    datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0") if hhmm else ""
)
templates.env.filters["state"] = lambda s: (s or "").replace("STATE_", "").replace("_", " ").lower()


def bluebubbles_check(bb: BlueBubblesMessenger) -> tuple[str, str]:
    """Ping the server (raises MessagingError) and describe what it reports.
    Returns (message, flash kind)."""
    bb.ping()
    info = bb.server_info()
    parts = [f"BlueBubbles server at {bb.base_url} answered"]
    if info.get("server_version"):
        parts[0] += f" (v{info['server_version']})"
    if info.get("detected_imessage"):
        parts.append(f"iMessage account: {info['detected_imessage']}")
    kind = "ok"
    if bb.private_api:
        if info and not (info.get("private_api") and info.get("helper_connected")):
            kind = "error"
            parts.append("Private API is on in Bruno but the helper isn't connected on the Mac, "
                         "so typing bubbles and sends will fail. Finish the Private API setup or turn it off here")
        elif info:
            parts.append("Private API helper connected")
    return ". ".join(parts) + ".", kind


def mount_dashboard(app: FastAPI, get_factory: Callable[[], sessionmaker], get_deps: Callable[[], Deps]) -> None:
    app.mount("/admin/static", StaticFiles(directory=str(HERE / "static")), name="admin-static")
    router = APIRouter(prefix="/admin")

    def db():
        session = get_factory()()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def admin(request: Request) -> None:
        if not request.session.get("admin"):
            raise HTTPException(status_code=303, headers={"Location": "/admin/login"})

    def flash(request: Request, message: str, kind: str = "ok") -> None:
        request.session.setdefault("flash", []).append([kind, message])

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        deps = get_deps()
        messages = request.session.pop("flash", [])
        return templates.TemplateResponse(
            request, name, {"s": deps.settings, "flashes": messages, "path": request.url.path, **ctx}
        )

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    # ------------------------------------------------------------------ auth
    @router.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, session: Session = Depends(db)):
        first_run = not configstore.has_admin_password(session, get_deps().settings.admin_password)
        return render(request, "login.html", first_run=first_run)

    @router.post("/login")
    def login(request: Request, password: str = Form(...), session: Session = Depends(db)):
        env_pw = get_deps().settings.admin_password
        if not configstore.has_admin_password(session, env_pw):
            if len(password) < 8:
                flash(request, "Pick a password with at least 8 characters.", "error")
                return back("/admin/login")
            configstore.set_admin_password(session, password)
        elif not configstore.check_admin_password(session, password, env_pw):
            flash(request, "Wrong password.", "error")
            return back("/admin/login")
        request.session["admin"] = True
        return back("/admin")

    @router.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return back("/admin/login")

    # ------------------------------------------------------------------ overview
    @router.get("", response_class=HTMLResponse, dependencies=[Depends(admin)])
    def overview(request: Request, session: Session = Depends(db)):
        deps = get_deps()
        s = deps.settings
        now = utcnow()
        customers = session.scalar(select(func.count(Customer.id))) or 0
        orders = session.scalar(select(func.count(Order.id))) or 0
        checklist = [
            ("AI model key", s.llm_ready and getattr(deps.llm, "name", "") != "mock", "/admin/settings#ai-model"),
            ("Texting gateway (BlueBubbles or Sendblue)", s.bluebubbles_ready or s.sendblue_ready,
             "/admin/settings#texting-bluebubbles"),
            ("Order history imported", orders > 0, "/admin/import"),
            ("Menu reviewed", bool(s.menu_json), "/admin/menu"),
            ("POS connected (or console tickets)", True, "/admin/settings#pos-square"),
            ("Autopilot on", s.autopilot_enabled, "/admin/outreach"),
        ]
        attention = session.scalars(
            select(Conversation).where(Conversation.needs_attention.is_(True)).order_by(Conversation.updated_at.desc())
        ).all()
        recent = session.scalars(select(Conversation).order_by(Conversation.updated_at.desc()).limit(8)).all()
        return render(
            request, "overview.html",
            stats=learning.summary(session, s, now), checklist=checklist, customers=customers, orders=orders,
            segments=importer.group_counts(session), attention=attention, recent=recent,
            llm_name=getattr(deps.llm, "name", "?"),
        )

    # ------------------------------------------------------------------ settings
    @router.get("/settings", response_class=HTMLResponse, dependencies=[Depends(admin)])
    def settings_page(request: Request, session: Session = Depends(db)):
        deps = get_deps()
        s = deps.settings
        saved = configstore.read_overrides(session)
        groups: dict[str, list] = {}
        for f in FIELDS:
            value = getattr(s, f.key)
            groups.setdefault(f.group, []).append(
                {"f": f, "value": "" if value is None else value, "is_set": value not in (None, ""), "from_db": f.key in saved}
            )
        base = (s.public_base_url or str(request.base_url)).rstrip("/")
        qs = f"?secret={s.webhook_secret}" if s.webhook_secret else ""
        urls = {
            "BlueBubbles: webhook (event: New Messages)": f"{base}/webhook/bluebubbles{qs}",
            "Sendblue: receive webhook": f"{base}/webhook/incoming{qs}",
            "Sendblue: status callback": f"{base}/webhook/sendblue/status{qs}",
            "Retell: inbound call webhook": f"{base}/voice/retell/inbound{qs}",
            "Retell: update_cart function": f"{base}/voice/retell/functions/update_cart{qs}",
            "Retell: place_order function": f"{base}/voice/retell/functions/place_order{qs}",
            "Stripe: webhook (event checkout.session.completed)": f"{base}/webhook/stripe",
            "POS order feed (optional)": f"{base}/webhook/pos/order{qs}",
        }
        status = {
            "AI": getattr(deps.llm, "name", "?"),
            "Texting": type(deps.messenger).__name__.replace("Messenger", ""),
            "POS": type(deps.pos).__name__.replace("POS", "") or "Console",
            "Payments": "Stripe" + ("" if deps.payments.supports_links else " (set public URL for pay links)")
            if type(deps.payments).__name__ == "StripePayments" else "Pay at pickup",
            "Delivery": "DoorDash Drive" if deps.delivery else (
                "Store links: " + ", ".join(n for n, _ in marketplace_links(s)) if marketplace_links(s) else "Pickup only"
            ),
        }
        return render(request, "settings.html", groups=groups, urls=urls, status=status)

    @router.post("/settings", dependencies=[Depends(admin)])
    async def save_settings(request: Request, session: Session = Depends(db)):
        form = await request.form()
        updates: dict[str, str | None] = {}
        for f in FIELDS:
            if f.key not in form and f.kind != "bool":
                continue
            raw = str(form.get(f.key, "")).strip()
            if f.secret:
                if form.get(f"clear_{f.key}"):
                    updates[f.key] = None
                elif raw:
                    updates[f.key] = raw
                continue
            if f.kind == "bool":
                if f"{f.key}__present" in form:
                    updates[f.key] = "true" if form.get(f.key) == "true" else "false"
                continue
            updates[f.key] = raw or None
        try:
            configstore.set_values(session, updates)
            session.commit()
            configstore.reload_runtime()
            get_deps()  # rebuild providers now so bad keys surface immediately
            flash(request, "Saved. Bruno is using the new settings.")
        except Exception as exc:
            session.rollback()
            configstore.reload_runtime()
            flash(request, f"Couldn't apply settings: {exc}", "error")
        return back("/admin/settings")

    @router.post("/settings/test/{service}", dependencies=[Depends(admin)])
    def test_service(service: str, request: Request):
        deps = get_deps()
        s = deps.settings
        try:
            if service == "ai":
                reply = deps.llm.write(
                    "You are a friendly pizzeria manager. Reply with one short casual sentence.",
                    "Say hi to the owner and confirm you're ready to take orders.",
                )
                flash(request, f"AI says: “{reply}”")
            elif service == "text":
                if not s.owner_phone:
                    raise ValueError("Add the owner/manager cell first.")
                deps.messenger.send(normalize_phone(s.owner_phone), f"👋 Test from Bruno at {s.restaurant_name}. Texting works!")
                flash(request, f"Test text sent to {s.owner_phone}.")
            elif service == "bluebubbles":
                bb = deps.messenger if isinstance(deps.messenger, BlueBubblesMessenger) else None
                if bb is None and not s.bluebubbles_ready:
                    raise ValueError("Add the BlueBubbles server URL and password first, then Save.")
                flash(request, *bluebubbles_check(bb or BlueBubblesMessenger(s)))
            else:
                raise ValueError("unknown test")
        except (LLMError, MessagingError, ValueError) as exc:
            flash(request, f"Test failed: {exc}", "error")
        return back("/admin/settings")

    # ------------------------------------------------------------------ import
    @router.get("/import", response_class=HTMLResponse, dependencies=[Depends(admin)])
    def import_page(request: Request):
        return render(request, "import.html", report=None)

    @router.post("/import", response_class=HTMLResponse, dependencies=[Depends(admin)])
    async def do_import(
        request: Request, file: UploadFile, source: str = Form("pos"), consent: str | None = Form(None),
        session: Session = Depends(db),
    ):
        deps = get_deps()
        content = await file.read()
        report = importer.import_csv(
            session, content, deps.menu, deps.settings, source=source, consented=bool(consent)
        )
        return render(request, "import.html", report=report, unmatched=importer.unmatched_summary(report))

    # ------------------------------------------------------------------ menu
    @router.get("/menu", response_class=HTMLResponse, dependencies=[Depends(admin)])
    def menu_page(request: Request):
        deps = get_deps()
        s = deps.settings
        raw = s.menu_json or Path(s.menu_path).read_text()
        return render(request, "menu.html", menu=deps.menu, raw=json.dumps(json.loads(raw), indent=2), money=money)

    @router.post("/menu", dependencies=[Depends(admin)])
    def save_menu(request: Request, menu_json: str = Form(...), reset: str | None = Form(None), session: Session = Depends(db)):
        if reset:
            configstore.set_values(session, {"menu_json": None})
            flash(request, "Menu reset to the default file.")
            return back("/admin/menu")
        try:
            data = json.loads(menu_json)
            Menu(data, get_deps().settings.tax_rate)
        except Exception as exc:
            flash(request, f"Menu not saved: {exc}", "error")
            return back("/admin/menu")
        configstore.set_values(session, {"menu_json": json.dumps(data)})
        flash(request, "Menu saved.")
        return back("/admin/menu")

    # ------------------------------------------------------------------ customers
    @router.get("/customers", response_class=HTMLResponse, dependencies=[Depends(admin)])
    def customers_page(request: Request, segment: str = "", q: str = "", session: Session = Depends(db)):
        stmt = select(Customer, CadenceTracker).outerjoin(CadenceTracker, CadenceTracker.customer_id == Customer.id)
        if segment:
            stmt = stmt.where(CadenceTracker.segment == segment)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(Customer.name.ilike(like) | Customer.phone_number.ilike(like))
        rows = session.execute(stmt.order_by(Customer.lifetime_value.desc()).limit(300)).all()
        menu = get_deps().menu
        return render(
            request, "customers.html", rows=rows, segment=segment, q=q, describe=menu.describe,
            segments=importer.group_counts(session),
        )

    @router.get("/customers/{customer_id}", response_class=HTMLResponse, dependencies=[Depends(admin)])
    def customer_page(customer_id: int, request: Request, session: Session = Depends(db)):
        customer = session.get(Customer, customer_id)
        if not customer:
            raise HTTPException(404)
        deps = get_deps()
        orders = session.scalars(
            select(Order).where(Order.customer_id == customer_id).order_by(Order.order_date.desc()).limit(50)
        ).all()
        convs = session.scalars(
            select(Conversation).where(Conversation.customer_id == customer_id).order_by(Conversation.id.desc()).limit(20)
        ).all()
        sent = session.scalars(
            select(Outreach).where(Outreach.customer_id == customer_id).order_by(Outreach.id.desc()).limit(20)
        ).all()
        local = to_local(utcnow(), deps.settings.restaurant_timezone)
        return render(
            request, "customer.html", c=customer, t=customer.cadence, orders=orders, convs=convs, sent=sent,
            describe=deps.menu.describe, payment=payment_method_label(customer),
            ideal=outreach.ideal_send_time(customer, deps, local).strftime("%I:%M %p").lstrip("0"),
        )

    @router.post("/customers/{customer_id}/memory", dependencies=[Depends(admin)])
    def save_memory(customer_id: int, request: Request, memory: str = Form(""), session: Session = Depends(db)):
        customer = session.get(Customer, customer_id)
        customer.memory = [line.strip() for line in memory.splitlines() if line.strip()][:15]
        flash(request, "Saved what Bruno remembers.")
        return back(f"/admin/customers/{customer_id}")

    @router.post("/customers/{customer_id}/optout", dependencies=[Depends(admin)])
    def toggle_optout(customer_id: int, request: Request, session: Session = Depends(db)):
        customer = session.get(Customer, customer_id)
        customer.opted_out = not customer.opted_out
        flash(request, "Opted out: Bruno won't text them." if customer.opted_out else "Opted back in.")
        return back(f"/admin/customers/{customer_id}")

    @router.post("/customers/{customer_id}/reach-out", dependencies=[Depends(admin)])
    def reach_out_now(customer_id: int, request: Request, mode: str = Form("approve"), session: Session = Depends(db)):
        customer = session.get(Customer, customer_id)
        deps = get_deps()
        if cadence.open_conversation(session, customer_id, deps.settings):
            flash(request, "They're already mid-conversation.", "error")
            return back(f"/admin/customers/{customer_id}")
        o = outreach.reach_out(session, deps, customer, utcnow(), mode=mode)
        if o is None:
            flash(request, "Can't text them (opted out, or no order history to go on).", "error")
        elif o.status == "pending_approval":
            flash(request, "Draft is waiting in Outreach for your OK.")
        else:
            flash(request, f"Sent: “{o.message}”" if o.status == "sent" else f"Not sent ({o.status}).")
        return back(f"/admin/customers/{customer_id}")

    # ------------------------------------------------------------------ conversations
    @router.get("/conversations", response_class=HTMLResponse, dependencies=[Depends(admin)])
    def conversations_page(request: Request, session: Session = Depends(db)):
        convs = session.scalars(
            select(Conversation).order_by(Conversation.needs_attention.desc(), Conversation.updated_at.desc()).limit(150)
        ).all()
        last = {}
        for conv in convs:
            m = session.scalars(
                select(Message).where(Message.conversation_id == conv.id, Message.role != "system")
                .order_by(Message.id.desc()).limit(1)
            ).first()
            last[conv.id] = m
        return render(request, "conversations.html", convs=convs, last=last, terminal={s.value for s in TERMINAL})

    @router.get("/conversations/{conv_id}", response_class=HTMLResponse, dependencies=[Depends(admin)])
    def conversation_page(conv_id: int, request: Request, session: Session = Depends(db)):
        conv = session.get(Conversation, conv_id)
        if not conv:
            raise HTTPException(404)
        deps = get_deps()
        validated = deps.menu.validate(conv.cart_json or [])
        totals = order_totals(validated, conv.fulfillment, deps.settings)
        summary = order_summary(validated, conv.pickup_time, payment_method_label(conv.customer), totals.delivery_fee_cents) if validated.lines else ""
        order = session.scalars(select(Order).where(Order.conversation_id == conv_id)).first()
        return render(
            request, "conversation.html", conv=conv, summary=summary, order=order,
            local=lambda dt: display_local(dt, deps.settings.restaurant_timezone),
            active=State(conv.state) not in TERMINAL,
        )

    @router.post("/conversations/{conv_id}/reply", dependencies=[Depends(admin)])
    def staff_reply(conv_id: int, request: Request, text: str = Form(...), session: Session = Depends(db)):
        conv = session.get(Conversation, conv_id)
        try:
            Bruno(session, get_deps()).staff_reply(conv, text.strip())
            flash(request, "Sent. Bruno is paused on this conversation until you hand it back.")
        except MessagingError as exc:
            flash(request, f"Couldn't send: {exc}", "error")
        return back(f"/admin/conversations/{conv_id}")

    @router.post("/conversations/{conv_id}/takeover", dependencies=[Depends(admin)])
    def takeover(conv_id: int, request: Request, on: str = Form("1"), session: Session = Depends(db)):
        conv = session.get(Conversation, conv_id)
        conv.human_takeover = on == "1"
        conv.needs_attention = False
        flash(request, "You've got it. Bruno will stay quiet." if conv.human_takeover else "Handed back to Bruno.")
        return back(f"/admin/conversations/{conv_id}")

    # ------------------------------------------------------------------ outreach
    @router.get("/outreach", response_class=HTMLResponse, dependencies=[Depends(admin)])
    def outreach_page(request: Request, session: Session = Depends(db)):
        deps = get_deps()
        now = utcnow()
        pending = session.scalars(
            select(Outreach).where(Outreach.status == "pending_approval").order_by(Outreach.created_at)
        ).all()
        recent = session.scalars(
            select(Outreach).where(Outreach.status == "sent").order_by(Outreach.sent_at.desc()).limit(40)
        ).all()
        local = to_local(now, deps.settings.restaurant_timezone)
        due = [
            (c, outreach.ideal_send_time(c, deps, local))
            for c in cadence.outreach_candidates(session, deps.settings, now)[:40]
        ]
        arms = sorted(learning.arm_stats(session, deps.settings, now).values(), key=lambda a: -a.sent)
        return render(
            request, "outreach.html", pending=pending, recent=recent, due=due, arms=arms,
            stats=learning.summary(session, deps.settings, now), local=local, describe=deps.menu.describe,
        )

    @router.post("/outreach/autopilot", dependencies=[Depends(admin)])
    def set_autopilot(request: Request, enabled: str = Form(...), mode: str = Form("approve"), session: Session = Depends(db)):
        configstore.set_values(session, {"autopilot_enabled": "true" if enabled == "1" else "false", "outreach_mode": mode})
        flash(request, ("Autopilot on" if enabled == "1" else "Autopilot off") + (f" ({mode} mode)." if enabled == "1" else "."))
        return back("/admin/outreach")

    @router.post("/outreach/run", dependencies=[Depends(admin)])
    def run_now(request: Request, count: int = Form(5), session: Session = Depends(db)):
        deps = get_deps()
        now = utcnow()
        made = 0
        for customer in cadence.outreach_candidates(session, deps.settings, now):
            if made >= count:
                break
            if session.scalars(
                select(Outreach.id).where(Outreach.customer_id == customer.id, Outreach.status == "pending_approval")
            ).first():
                continue
            if outreach.reach_out(session, deps, customer, now):
                made += 1
        verb = "drafted for your OK" if deps.settings.outreach_mode == "approve" else "sent"
        flash(request, f"{made} text(s) {verb}.")
        return back("/admin/outreach")

    @router.post("/outreach/{outreach_id}/approve", dependencies=[Depends(admin)])
    def approve(outreach_id: int, request: Request, message: str = Form(""), session: Session = Depends(db)):
        o = outreach.approve(session, get_deps(), outreach_id, utcnow(), message or None)
        if o is None:
            flash(request, "That draft is no longer pending.", "error")
        elif o.status == "sent":
            flash(request, "Sent.")
        else:
            flash(request, f"Not sent ({o.status}): they're mid-conversation or opted out.", "error")
        return back("/admin/outreach")

    @router.post("/outreach/{outreach_id}/skip", dependencies=[Depends(admin)])
    def skip(outreach_id: int, request: Request, session: Session = Depends(db)):
        outreach.skip(session, outreach_id)
        flash(request, "Skipped.")
        return back("/admin/outreach")

    app.include_router(router)

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/admin", status_code=303)
