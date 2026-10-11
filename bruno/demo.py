"""Demo data: a pizzeria's worth of customers with realistic order histories,
so the dashboard, learning and outreach have something to chew on."""

import random
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from bruno import cadence
from bruno.config import Settings
from bruno.menu import Menu
from bruno.messaging import normalize_phone
from bruno.models import Customer, Order
from bruno.timeutil import utcnow

FIRST = ["Maria", "Sam", "Priya", "Jake", "Lena", "Omar", "Chris", "Tasha", "Ben", "Rosa", "Kevin", "Ana",
         "Luis", "Grace", "Tom", "Nina", "Derek", "Ivy", "Marcus", "Julia", "Ray", "Mei", "Andre", "Kate",
         "Noah", "Zoe", "Eli", "Sara"]
LAST = ["Lopez", "Patel", "Nguyen", "Kim", "Russo", "Brown", "Cohen", "Ali", "Moore", "Diaz", "Walsh", "Chen"]
CARTS = [
    [{"item_id": "pizza", "size": "large", "modifiers": ["pepperoni"]}],
    [{"item_id": "pizza", "size": "large", "modifiers": ["pepperoni"]}, {"item_id": "garlic_knots"}],
    [{"item_id": "pizza", "size": "medium", "modifiers": ["mushroom", "onion"]}, {"item_id": "coke", "quantity": 2}],
    [{"item_id": "margherita", "size": "large"}, {"item_id": "caesar_salad"}],
    [{"item_id": "supreme", "size": "large"}, {"item_id": "garlic_knots"}],
    [{"item_id": "wings", "modifiers": ["buffalo_sauce"]}, {"item_id": "garlic_knots"}],
    [{"item_id": "pizza", "size": "large", "modifiers": ["sausage"]}, {"item_id": "wings", "modifiers": ["bbq_sauce"]}],
    [{"item_id": "pizza", "size": "small"}, {"item_id": "sprite"}],
    [{"item_id": "margherita", "size": "medium"}, {"item_id": "cannoli", "quantity": 2}],
]


def _norm(cart: list[dict]) -> list[dict]:
    return [{"item_id": i["item_id"], "size": i.get("size"), "quantity": i.get("quantity", 1),
             "modifiers": i.get("modifiers", []), "notes": None} for i in cart]


def seed_demo(session: Session, menu: Menu, settings: Settings, your_phone: str | None = None, n: int = 30,
              seed: int = 7) -> list[Customer]:
    rng = random.Random(seed)
    now = utcnow()
    created: list[Customer] = []
    profiles = [("Dave Rossi", your_phone or "+15555550100", _norm(CARTS[0]), 7, 21, 9, True, ["likes it well done"])]
    for k in range(n):
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        kind = rng.choices(["regular", "drifting", "lapsed", "one_time"], [4, 4, 2, 2])[0]
        every = rng.choice([5, 7, 7, 10, 14, 21])
        last = {
            "regular": rng.randint(1, max(2, every - 1)),
            "drifting": int(every * rng.uniform(1.5, 2.8)),
            "lapsed": rng.randint(70, 160),
            "one_time": rng.randint(18, 90),
        }[kind]
        count = 1 if kind == "one_time" else rng.randint(3, 14)
        profiles.append((name, f"+1555555{1000 + k:04d}", _norm(rng.choice(CARTS)), every, last, count,
                         rng.random() < 0.6, []))

    for name, phone, cart, every, last, count, card, memory in profiles:
        phone = normalize_phone(phone)
        if session.query(Customer).filter_by(phone_number=phone).first():
            continue
        customer = Customer(
            name=name, phone_number=phone, created_at=now - timedelta(days=last + every * count + 30),
            payment_token="tok_demo" if card else None, card_label="Visa •4242" if card else None, memory=memory,
        )
        session.add(customer)
        session.flush()
        total = Decimal(menu.validate(cart).total_cents) / 100
        hour = rng.choice(["17:30", "18:00", "18:30", "19:00", "12:00"])
        for i in range(count):
            when = now - timedelta(days=last + i * every + rng.uniform(-0.8, 0.8))
            this_cart = cart if rng.random() < 0.75 else _norm(rng.choice(CARTS))
            session.add(Order(
                customer_id=customer.id, items_json=this_cart, total_amount=total if this_cart is cart
                else Decimal(menu.validate(this_cart).total_cents) / 100,
                order_date=when, channel="pos", source="demo", status="completed", pickup_time=hour,
                external_id=f"demo-{phone}-{i}",
            ))
        session.flush()
        cadence.recompute_customer(session, customer, settings, now)
        created.append(customer)
    return created
