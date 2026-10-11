from bruno.importer import detect_columns, import_csv
from bruno.models import Customer, Order

SQUARE_STYLE = """Date,Time,Transaction ID,Customer Name,Customer Phone,Item,Qty,Gross Sales
2026-09-04,18:05,T1,Dave Rossi,(555) 123-0001,Lg Pepperoni Pizza,1,$20.50
2026-09-04,18:05,T1,Dave Rossi,(555) 123-0001,Garlic Knots,1,$5.99
2026-09-11,18:02,T2,Dave Rossi,(555) 123-0001,Large Pepperoni Pizza,1,$20.50
2026-09-18,17:58,T3,Dave Rossi,555-123-0001,16" pepperoni pizza,1,$20.50
2026-09-18,12:00,T4,Walk In,,Coke,1,$2.99
2026-09-20,19:00,T5,Maria Lopez,5551230002,Buffalo Wings (10),1,$13.99
2026-09-20,19:00,T5,Maria Lopez,5551230002,Mystery Calzone,1,$12.00
"""

ORDER_LEVEL = """order_number,created_at,phone,name,items,order_total
A1,09/01/2026 6:30 PM,+15551230003,Sam Patel,"1x Medium Margherita Pizza, 2x Coke",$25.00
A2,09/08/2026 6:30 PM,+15551230003,Sam Patel,"Medium Margherita Pizza; Coke x2",$25.00
"""


def test_detect_columns():
    cols = detect_columns(["Date", "Customer Phone", "Item", "Transaction ID", "Gross Sales"])
    assert cols == {"phone": "Customer Phone", "date": "Date", "order_id": "Transaction ID", "items": "Item", "total": "Gross Sales"}


def test_line_item_export(session, deps, settings):
    r = import_csv(session, SQUARE_STYLE, deps.menu, settings, source="square")
    assert r.orders_created == 4 and r.customers_created == 2 and r.rows_without_phone == 1
    assert r.unmatched_items["mystery calzone"] == 1
    dave = session.query(Customer).filter_by(phone_number="+15551230001").one()
    assert dave.name == "Dave Rossi" and dave.total_orders == 3
    first = session.query(Order).filter_by(external_id="square:T1").one()
    assert [i["item_id"] for i in first.items_json] == ["pizza", "garlic_knots"]
    assert first.items_json[0]["size"] == "large" and first.items_json[0]["modifiers"] == ["pepperoni"]
    assert float(first.total_amount) == 26.49
    assert dave.cadence.usual_order_json[0]["modifiers"] == ["pepperoni"]
    assert round(dave.cadence.avg_days_between_orders) == 7

    again = import_csv(session, SQUARE_STYLE, deps.menu, settings, source="square")
    assert again.orders_created == 0 and again.orders_skipped_duplicate == 4


def test_order_level_export_with_quantities(session, deps, settings):
    r = import_csv(session, ORDER_LEVEL, deps.menu, settings, source="toast")
    assert r.orders_created == 2
    order = session.query(Order).filter_by(external_id="toast:A2").one()
    assert {(i["item_id"], i["quantity"]) for i in order.items_json} == {("margherita", 1), ("coke", 2)}


def test_no_consent_imports_but_never_texts(session, deps, settings):
    import_csv(session, ORDER_LEVEL, deps.menu, settings, source="toast", consented=False)
    assert session.query(Customer).one().opted_out


def test_bad_file(session, deps, settings):
    r = import_csv(session, "foo,bar\n1,2\n", deps.menu, settings)
    assert r.errors and r.orders_created == 0
