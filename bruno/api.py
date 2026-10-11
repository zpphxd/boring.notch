"""The Bruno web app: webhooks, voice tools, owner dashboard and scheduler.

Run: python -m bruno   (or: uvicorn bruno.api:app)
"""

import asyncio
import hmac
import html
import json
import logging
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from decimal import Decimal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from bruno import cadence, configstore, learning
from bruno.agent import Bruno
from bruno.config import env_settings
from bruno.dashboard import mount_dashboard
from bruno.db import get_engine, get_sessionmaker, init_db
from bruno.deps import Deps, get_deps
from bruno.locks import customer_lock
from bruno.menu import CartItem
from bruno.messaging import normalize_phone, parse_sendblue_inbound
from bruno.models import Conversation, Customer, Order
from bruno.payments import verify_stripe_signature
from bruno.timeutil import utcnow
from bruno.voice import dynamic_variables

log = logging.getLogger("bruno.api")


class POSOrderIngest(BaseModel):
    """Live order feed from the POS (webhook relay, Zapier, nightly export)."""

    phone: str
    name: str | None = None
    external_id: str | None = None
    items: list[CartItem] = []
    item_text: str | None = None  # alternatively, free text like "Lg Pepperoni Pizza, 2 Coke"
    total: Decimal | None = None
    order_date: datetime | None = None  # UTC
    pickup_time: str | None = None
    source: str = "pos"


def create_app(deps: Deps | None = None, session_factory: sessionmaker | None = None) -> FastAPI:
    def get_app_deps() -> Deps:
        return deps or get_deps()

    def get_factory() -> sessionmaker:
        return session_factory or get_sessionmaker()

    if session_factory is None:
        init_db(get_engine())
        configstore.reload_runtime()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = None
        if session_factory is None and env_settings().scheduler_enabled:
            from bruno.scheduler import run_forever

            task = asyncio.create_task(run_forever())
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Bruno", version="0.2.0", lifespan=lifespan)

    with get_factory()() as s:
        secret = configstore.session_secret(s, env_settings().secret_key)
        s.commit()
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="lax", max_age=60 * 60 * 24 * 14)

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

    def verify_secret(request: Request) -> None:
        secret = get_app_deps().settings.webhook_secret
        if not secret:
            return
        given = request.query_params.get("secret") or request.headers.get("x-bruno-secret") or ""
        if not hmac.compare_digest(given, secret):
            raise HTTPException(status_code=401, detail="bad webhook secret")

    @app.get("/health")
    def health() -> dict:
        d = get_app_deps()
        return {
            "ok": True,
            "llm": getattr(d.llm, "name", type(d.llm).__name__),
            "messaging": type(d.messenger).__name__,
            "pos": type(d.pos).__name__,
            "delivery": type(d.delivery).__name__ if d.delivery else None,
        }

    # ------------------------------------------------------------------ text channel
    def process_inbound_text(phone: str, content: str, message_id: str | None) -> None:
        with customer_lock(phone):
            session = get_factory()()
            try:
                outcome = Bruno(session, get_app_deps()).handle_inbound_text(phone, content, message_id)
                session.commit()
                log.info("inbound from %s -> state=%s", phone, outcome.state)
            except Exception:
                session.rollback()
                log.exception("failed to process inbound text from %s", phone)
            finally:
                session.close()

    @app.post("/webhook/incoming", dependencies=[Depends(verify_secret)])
    async def incoming_text(request: Request, background: BackgroundTasks) -> dict:
        payload = await request.json()
        inbound = parse_sendblue_inbound(payload)
        if inbound is None:
            return {"status": "ignored"}
        # Ack fast (Sendblue retries slow/non-2xx webhooks); think + "type" after.
        background.add_task(process_inbound_text, inbound.from_number, inbound.content, inbound.message_id)
        return {"status": "accepted"}

    @app.post("/webhook/sendblue/status", dependencies=[Depends(verify_secret)])
    async def sendblue_status(request: Request) -> dict:
        payload = await request.json()
        log.info(
            "sendblue status %s for %s: %s %s", payload.get("message_handle"), payload.get("number"),
            payload.get("status"), payload.get("error_message") or "",
        )
        return {"status": "ok"}

    # ------------------------------------------------------------------ POS ingestion
    @app.post("/webhook/pos/order", dependencies=[Depends(verify_secret)])
    def ingest_pos_order(body: POSOrderIngest, session: Session = Depends(db)) -> dict:
        d = get_app_deps()
        agent = Bruno(session, d)
        customer = agent.get_or_create_customer(body.phone)
        if body.name and not customer.name:
            customer.name = body.name
        if body.external_id and session.scalars(select(Order).where(Order.external_id == body.external_id)).first():
            return {"status": "duplicate"}
        items = list(body.items)
        if not items and body.item_text:
            from bruno.importer import parse_items
            from collections import Counter

            items = parse_items(body.item_text, 1, d.menu, Counter())
        validated = d.menu.validate(items)
        total = body.total if body.total is not None else Decimal(validated.total_cents) / 100
        order = Order(
            customer_id=customer.id,
            items_json=[i.model_dump() for i in validated.items()],
            raw_items=body.item_text,
            total_amount=total,
            order_date=body.order_date or utcnow(),
            channel="pos",
            source=body.source,
            status="completed",
            pickup_time=body.pickup_time,
            external_id=body.external_id,
        )
        session.add(order)
        session.flush()
        won = learning.attribute_order(session, order, d.settings)
        tracker = cadence.recompute_customer(session, customer, d.settings)
        return {
            "status": "ok", "customer_id": customer.id, "segment": tracker.segment,
            "drifting": tracker.drift_threshold_flag, "attributed_to_outreach": won.id if won else None,
        }

    # ------------------------------------------------------------------ payments (Stripe)
    @app.post("/webhook/stripe")
    async def stripe_webhook(request: Request, session: Session = Depends(db)) -> dict:
        d = get_app_deps()
        secret = d.settings.stripe_webhook_secret
        payload = await request.body()
        if not secret or not verify_stripe_signature(payload, request.headers.get("stripe-signature", ""), secret):
            raise HTTPException(status_code=400, detail="bad signature")
        event = json.loads(payload)
        obj = event.get("data", {}).get("object", {})
        if event.get("type") != "checkout.session.completed" or obj.get("payment_status") != "paid":
            return {"status": "ignored"}
        conv_id = (obj.get("metadata") or {}).get("conversation_id")
        conv = session.get(Conversation, int(conv_id)) if conv_id and str(conv_id).isdigit() else None
        if not conv:
            return {"status": "unknown conversation"}
        intent_id = obj.get("payment_intent")
        saved = None
        if hasattr(d.payments, "saved_card_from") and intent_id:
            try:
                saved = d.payments.saved_card_from(intent_id)
            except Exception:
                log.exception("couldn't load saved card for %s", intent_id)
        customer = session.get(Customer, conv.customer_id)
        with customer_lock(customer.phone_number):
            result = Bruno(session, d).payment_received(conv, intent_id, saved)
        return {"status": "ok", "placed": bool(result and result.ok)}

    @app.get("/paid", response_class=HTMLResponse, include_in_schema=False)
    def paid_page() -> str:
        name = get_app_deps().settings.restaurant_name
        return (
            "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<body style='font:18px -apple-system,sans-serif;text-align:center;padding:15vh 20px'>"
            f"<h1>🍕 Thanks!</h1><p>Your order is headed to the kitchen at {html.escape(name)}.<br>"
            "We'll text you when it's on the way.</p>"
        )

    # ------------------------------------------------------------------ voice channel (Retell)
    @app.post("/voice/retell/inbound", dependencies=[Depends(verify_secret)])
    async def retell_inbound(request: Request, session: Session = Depends(db)) -> dict:
        payload = await request.json()
        call = payload.get("call_inbound") or payload
        from_number = call.get("from_number")
        if not from_number:
            raise HTTPException(status_code=400, detail="missing from_number")
        d = get_app_deps()
        ctx = Bruno(session, d).voice_context(from_number)
        return {
            "call_inbound": {
                "dynamic_variables": dynamic_variables(ctx, d.settings, d.menu),
                "metadata": {"customer_id": ctx.customer.id},
            }
        }

    @app.post("/voice/retell/functions/{name}", dependencies=[Depends(verify_secret)])
    async def retell_function(name: str, request: Request, session: Session = Depends(db)) -> dict:
        payload = await request.json()
        call = payload.get("call") or {}
        args = payload.get("args") or {}
        phone = call.get("from_number") or args.get("phone")
        if not phone:
            raise HTTPException(status_code=400, detail="cannot identify caller (disable 'args only' payloads)")
        phone = normalize_phone(phone)
        agent = Bruno(session, get_app_deps())
        with customer_lock(phone):
            if name == "update_cart":
                return agent.voice_update_cart(
                    phone, args.get("items") or [], args.get("pickup_time"),
                    args.get("fulfillment"), args.get("delivery_address"),
                )
            if name == "place_order":
                return agent.voice_place_order(phone, bool(args.get("customer_confirmed")))
        raise HTTPException(status_code=404, detail=f"unknown function {name}")

    mount_dashboard(app, get_factory, get_app_deps)
    return app


def __getattr__(name: str):
    # `uvicorn bruno.api:app` builds the app lazily so importing this module stays side-effect free.
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
