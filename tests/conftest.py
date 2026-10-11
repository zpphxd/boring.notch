from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from bruno import cadence
from bruno.config import Settings
from bruno.db import Base
from bruno.deps import Deps
from bruno.llm.mock_llm import MockLLM
from bruno.menu import Menu
from bruno.messaging import ConsoleMessenger
from bruno.models import Customer, Order
from bruno.payments import MockPaymentProvider
from bruno.pos import ConsolePOS
from bruno.timeutil import utcnow

ROOT = Path(__file__).resolve().parent.parent
# Friday 2026-10-09, 4:30 PM in New York (EDT) == 20:30 UTC
FRIDAY_430PM = datetime(2026, 10, 9, 20, 30)
DAVE_PHONE = "+15551230001"
PEPPERONI = [{"item_id": "pizza", "size": "large", "quantity": 1, "modifiers": ["pepperoni"], "notes": None}]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="sqlite://",
        menu_path=str(ROOT / "data/menu.json"),
        llm_provider="mock",
        messaging_provider="console",
        pos_provider="console",
        include_opt_out_footer=True,
        webhook_secret=None,
    )


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    from bruno import models  # noqa: F401

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def session(session_factory):
    s = session_factory()
    yield s
    s.close()


@pytest.fixture
def deps(settings) -> Deps:
    return Deps(
        settings=settings,
        menu=Menu.load(settings.menu_path, settings.tax_rate),
        llm=MockLLM(),
        messenger=ConsoleMessenger(echo=False),
        pos=ConsolePOS(echo=False),
        payments=MockPaymentProvider(),
    )


def make_customer(
    session,
    settings,
    *,
    now=None,
    phone=DAVE_PHONE,
    name="Dave Rossi",
    cart=PEPPERONI,
    every_days=7,
    last_days_ago=21,
    count=6,
    card=True,
) -> Customer:
    now = now or utcnow()
    customer = Customer(
        name=name,
        phone_number=phone,
        payment_token="tok_test" if card else None,
        card_label="Visa •4242" if card else None,
    )
    session.add(customer)
    session.flush()
    for n in range(count):
        session.add(
            Order(
                customer_id=customer.id,
                items_json=cart,
                total_amount=Decimal("21.06"),
                order_date=now - timedelta(days=last_days_ago + n * every_days),
                channel="pos",
                status="completed",
                pickup_time="18:00",
            )
        )
    session.flush()
    cadence.recompute_customer(session, customer, settings, now)
    session.commit()
    return customer
