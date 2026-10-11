import os
import random
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


def make_settings(**overrides) -> Settings:
    base = dict(
        _env_file=None,
        database_url="sqlite://",
        menu_path=str(ROOT / "data/menu.json"),
        llm_provider="mock",
        messaging_provider="console",
        pos_provider="console",
        human_delays=False,
        webhook_secret=None,
        admin_password=None,
        restaurant_phone="+15550009999",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def session_factory():
    """In-memory SQLite by default; set BRUNO_TEST_DATABASE_URL to run against Postgres."""
    from bruno import models  # noqa: F401

    url = os.environ.get("BRUNO_TEST_DATABASE_URL")
    if url:
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    else:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


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
        delivery=None,
        rng=random.Random(1),
        sleep=lambda _: None,
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
        memory=[],
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
