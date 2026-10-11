"""FastAPI app: inbound text webhook, voice webhooks and POS order ingestion.

Run: uvicorn bruno.api:app --reload
"""

import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from bruno import cadence
from bruno.agent import Bruno
from bruno.db import get_engine, get_sessionmaker, init_db
from bruno.deps import Deps, get_deps
from bruno.locks import customer_lock
from bruno.menu import CartItem
from bruno.messaging import normalize_phone, parse_sendblue_inbound
from bruno.models import Order
from bruno.timeutil import utcnow
from bruno.voice import dynamic_variables

log = logging.getLogger("bruno.api")


class POSOrderIngest(BaseModel):
    """Generic order feed from the POS (nightly export, Zapier, POS webhook relay)."""

    phone: str
    name: str | None = None
    external_id: str | None = None
    items: list[CartItem]
    total: Decimal | None = None
    order_date: datetime | None = None  # UTC
    pickup_time: str | None = None


def create_app(deps: Deps | None = None, session_factory: sessionmaker | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if session_factory is None:
            init_db(get_engine())
        yield

    app = FastAPI(title="Bruno", version="0.1.0", lifespan=lifespan)

    def get_app_deps() -> Deps:
        return deps or get_deps()

    def get_factory() -> sessionmaker:
        return session_factory or get_sessionmaker()

    def db() -> Session:
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

    # ------------------------------------------------------------------ health
    @app.get("/health")
    def health() -> dict:
        d = get_app_deps()
        return {
            "ok": True,
            "llm": getattr(d.llm, "name", type(d.llm).__name__),
            "messaging": d.settings.messaging_provider,
            "pos": d.settings.pos_provider,
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
        # Ack fast (Sendblue retries slow/non-2xx webhooks); do the LLM work after.
        background.add_task(process_inbound_text, inbound.from_number, inbound.content, inbound.message_id)
        return {"status": "accepted"}

    @app.post("/webhook/sendblue/status", dependencies=[Depends(verify_secret)])
    async def sendblue_status(request: Request) -> dict:
        payload = await request.json()
        log.info(
            "sendblue status %s for %s: %s %s",
            payload.get("message_handle"),
            payload.get("number"),
            payload.get("status"),
            payload.get("error_message") or "",
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
        validated = d.menu.validate(body.items)
        total = body.total if body.total is not None else Decimal(validated.total_cents) / 100
        session.add(
            Order(
                customer_id=customer.id,
                items_json=[i.model_dump() for i in validated.items()] or [i.model_dump() for i in body.items],
                total_amount=total,
                order_date=body.order_date or utcnow(),
                channel="pos",
                status="completed",
                pickup_time=body.pickup_time,
                external_id=body.external_id,
            )
        )
        session.flush()
        tracker = cadence.recompute_customer(session, customer, d.settings)
        return {"status": "ok", "customer_id": customer.id, "drifting": tracker.drift_threshold_flag}

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
                return agent.voice_update_cart(phone, args.get("items") or [], args.get("pickup_time"))
            if name == "place_order":
                return agent.voice_place_order(phone, bool(args.get("customer_confirmed")))
        raise HTTPException(status_code=404, detail=f"unknown function {name}")

    return app


app = create_app()
