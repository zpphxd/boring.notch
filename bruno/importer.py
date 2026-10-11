"""Import order history from POS / marketplace CSV exports so Bruno can learn
each customer before sending a single text.

Works with Square, Toast, Clover and DoorDash exports and any other
spreadsheet: columns are detected by name, line-item exports are grouped
by order/check number, and item names are mapped onto the menu
("Lg Pepperoni Pizza" -> large pizza with pepperoni). Rows without a phone
number are skipped and counted in the report (marketplaces usually mask
phones, so their exports mostly help when they include contact details).
"""

import csv
import hashlib
import io
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser
from sqlalchemy import select
from sqlalchemy.orm import Session

from bruno import cadence
from bruno.config import Settings
from bruno.menu import CartItem, Menu
from bruno.messaging import normalize_phone
from bruno.models import Customer, Order
from bruno.timeutil import local_to_utc

COLUMNS = {
    "phone": ["phone", "phone number", "customer phone", "mobile", "cell", "contact phone", "guest phone",
              "customer phone number", "recipient phone"],
    "name": ["customer name", "name", "customer", "guest name", "guest", "recipient name", "diner name"],
    "first_name": ["first name", "customer first name", "guest first name"],
    "last_name": ["last name", "customer last name", "guest last name"],
    "date": ["order date", "date", "created at", "created", "order time", "placed at", "opened",
             "business date", "order placed", "order placed time", "date/time", "transaction date", "paid date"],
    "time": ["time", "time of day", "order time of day"],
    "order_id": ["order id", "order #", "order number", "check #", "check number", "check id", "receipt number",
                 "transaction id", "order", "delivery uuid", "doordash order id", "grubhub order id"],
    "items": ["items", "order items", "item", "item name", "menu item", "product", "description",
              "item description", "line item", "menu item name"],
    "qty": ["qty", "quantity", "item qty", "count"],
    "total": ["total", "order total", "total amount", "amount", "gross sales", "net sales", "subtotal",
              "total collected", "price", "item price", "net total"],
    "address": ["address", "delivery address", "street address", "dropoff address", "customer address"],
    "email": ["email", "customer email"],
}
ITEM_SPLIT = re.compile(r"\s*(?:[,;|\n]|\s\+\s)\s*")
QTY_PREFIX = re.compile(r"^\s*(\d+)\s*[x×]?\s+(.+)$", re.I)
QTY_SUFFIX = re.compile(r"^(.+?)\s*[x×]\s*(\d+)\s*$", re.I)


@dataclass
class ImportReport:
    rows: int = 0
    orders_created: int = 0
    orders_skipped_duplicate: int = 0
    rows_without_phone: int = 0
    customers_created: int = 0
    customers_updated: int = 0
    unmatched_items: Counter = field(default_factory=Counter)
    columns: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def detect_columns(headers: list[str]) -> dict[str, str]:
    norm = {h: re.sub(r"\s+", " ", h.strip().lower().replace("_", " ")) for h in headers}
    found: dict[str, str] = {}
    for field_name, aliases in COLUMNS.items():
        for alias in aliases:
            match = next((h for h, n in norm.items() if n == alias and h not in found.values()), None)
            if match:
                found[field_name] = match
                break
    return found


def parse_money(value: str | None) -> Decimal | None:
    if not value:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", value)
    try:
        return Decimal(cleaned) if cleaned else None
    except InvalidOperation:
        return None


def parse_items(text: str, qty: int, menu: Menu, unmatched: Counter) -> list[CartItem]:
    items: list[CartItem] = []
    for part in ITEM_SPLIT.split(text or ""):
        part = part.strip()
        if not part:
            continue
        n = 1
        if m := QTY_PREFIX.match(part):
            n, part = int(m.group(1)), m.group(2)
        elif m := QTY_SUFFIX.match(part):
            part, n = m.group(1), int(m.group(2))
        item = menu.parse_item_text(part, n * max(qty, 1))
        if item:
            items.append(item)
        else:
            unmatched[part.strip().lower()[:60]] += 1
    return items


def import_csv(
    session: Session, content: str | bytes, menu: Menu, settings: Settings, *, source: str = "pos",
    consented: bool = True,
) -> ImportReport:
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(content))
    report = ImportReport()
    cols = detect_columns(reader.fieldnames or [])
    report.columns = cols
    if "date" not in cols or "items" not in cols:
        report.errors.append(
            "Couldn't find an order date and an items column. Found: " + ", ".join(reader.fieldnames or [])
        )
        return report

    tz = ZoneInfo(settings.restaurant_timezone)
    grouped: dict[str, dict] = {}
    for row in reader:
        report.rows += 1
        get = lambda k: (row.get(cols[k]) or "").strip() if k in cols else ""  # noqa: E731
        raw_date = get("date")
        if get("time") and cols.get("time") != cols.get("date"):
            raw_date = f"{raw_date} {get('time')}"
        try:
            when = dateparser.parse(raw_date)
        except (ValueError, OverflowError):
            report.errors.append(f"row {report.rows}: bad date {raw_date!r}")
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=tz)
        phone = normalize_phone(get("phone")) if get("phone") else ""
        if phone and len(re.sub(r"\D", "", phone)) < 10:
            phone = ""
        order_key = get("order_id") or hashlib.sha1(f"{phone}|{raw_date}".encode()).hexdigest()[:16]
        g = grouped.setdefault(
            order_key,
            {"phone": phone, "name": "", "date": when, "items": [], "raw": [], "amounts": [],
             "address": get("address")},
        )
        name = get("name") or " ".join(p for p in (get("first_name"), get("last_name")) if p)
        g["name"] = g["name"] or name
        g["phone"] = g["phone"] or phone
        qty = int(get("qty")) if get("qty").isdigit() else 1
        g["items"].extend(parse_items(get("items"), qty, menu, report.unmatched_items))
        g["raw"].append(get("items"))
        amount = parse_money(get("total"))
        if amount is not None:
            g["amounts"].append(amount)

    touched: dict[int, Customer] = {}
    for key, g in grouped.items():
        if not g["phone"]:
            report.rows_without_phone += 1
            continue
        external_id = f"{source}:{key}"[:128]
        if session.scalars(select(Order.id).where(Order.external_id == external_id)).first():
            report.orders_skipped_duplicate += 1
            continue
        customer = session.scalars(select(Customer).where(Customer.phone_number == g["phone"])).first()
        if not customer:
            customer = Customer(phone_number=g["phone"], name=g["name"] or None, memory=[], opted_out=not consented)
            session.add(customer)
            session.flush()
            report.customers_created += 1
        else:
            if g["name"] and not customer.name:
                customer.name = g["name"]
            report.customers_updated += customer.id not in touched
        if g["address"] and not customer.address:
            customer.address = g["address"]
        items = _merge(g["items"])
        total = _order_total(g["amounts"], cols.get("total", "")) or Decimal(menu.validate(items).total_cents) / 100
        session.add(
            Order(
                customer_id=customer.id,
                items_json=[i.model_dump() for i in items],
                raw_items=" | ".join(r for r in g["raw"] if r)[:4000],
                total_amount=total,
                order_date=local_to_utc(g["date"]),
                channel="pos",
                source=source,
                status="completed",
                fulfillment="delivery" if source in ("doordash", "grubhub", "ubereats") else "pickup",
                external_id=external_id,
            )
        )
        touched[customer.id] = customer
        report.orders_created += 1

    session.flush()
    for customer in touched.values():
        cadence.recompute_customer(session, customer, settings)
    return report


def _order_total(amounts: list[Decimal], header: str) -> Decimal | None:
    if not amounts:
        return None
    order_level = header.strip().lower().replace("_", " ") in ("order total", "total", "total amount", "total collected")
    if len(amounts) > 1 and order_level and len(set(amounts)) == 1:
        return amounts[0]  # order total repeated on every line-item row
    return sum(amounts, Decimal(0))


def _merge(items: list[CartItem]) -> list[CartItem]:
    merged: dict[tuple, CartItem] = {}
    for item in items:
        key = (item.item_id, item.size, tuple(sorted(item.modifiers)))
        if key in merged:
            merged[key].quantity += item.quantity
        else:
            merged[key] = item.model_copy()
    return list(merged.values())


def unmatched_summary(report: ImportReport, limit: int = 15) -> list[tuple[str, int]]:
    return report.unmatched_items.most_common(limit)


def group_counts(session: Session) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for (segment,) in session.execute(select(cadence.CadenceTracker.segment)):
        counts[segment] += 1
    return dict(counts)
