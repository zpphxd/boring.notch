import pytest

from bruno.fsm import Intent, InvalidTransition, State, assert_transition, next_state
from bruno.llm.base import turn_json_schema
from bruno.menu import CartItem, Menu


@pytest.fixture
def menu(settings) -> Menu:
    return Menu.load(settings.menu_path, 0.08)


def test_prices_are_computed_server_side(menu):
    v = menu.validate(
        [
            CartItem(item_id="pizza", size="large", modifiers=["mushroom"]),
            CartItem(item_id="diet_coke", quantity=2),
        ]
    )
    assert v.ready
    assert [line.line_total_cents for line in v.lines] == [1800 + 250, 2 * 299]
    assert v.subtotal_cents == 2648
    assert v.tax_cents == round(2648 * 0.08)
    assert v.total_cents == v.subtotal_cents + v.tax_cents


@pytest.mark.parametrize(
    "item, fragment",
    [
        (CartItem(item_id="calzone"), "isn't on the menu"),
        (CartItem(item_id="pizza"), "What size"),
        (CartItem(item_id="coke", modifiers=["mushroom"]), "isn't available"),
        (CartItem(item_id="pizza", size="large", modifiers=["thin_crust", "gluten_free_crust"]), "Only one crust"),
        (CartItem(item_id="coke", quantity=0), "Quantity"),
    ],
)
def test_validation_errors(menu, item, fragment):
    v = menu.validate([item])
    assert not v.ready
    assert fragment in v.errors[0]


def test_describe(menu):
    cart = [
        {"item_id": "pizza", "size": "large", "modifiers": ["pepperoni"]},
        {"item_id": "diet_coke"},
    ]
    assert menu.describe(cart) == "large pepperoni pizza and Diet Coke"


def test_llm_schema_is_constrained_to_menu(menu):
    schema = turn_json_schema(menu)
    item = schema["properties"]["cart"]["items"]
    assert set(item["properties"]["item_id"]["enum"]) == set(menu.items)
    assert set(item["properties"]["modifiers"]["items"]["enum"]) == set(menu.modifiers)
    # OpenAI strict mode: every property required, no extras
    assert set(item["required"]) == set(item["properties"])
    assert schema["additionalProperties"] is False


def test_confirm_only_places_order_from_confirming_state():
    # "yes" to the outreach text shows a priced summary first
    assert (
        next_state(State.AWAITING_RESPONSE, Intent.CONFIRM_ORDER, cart_ready=True, cart_changed=False)
        == State.CONFIRMING_PAYMENT
    )
    assert (
        next_state(State.CONFIRMING_PAYMENT, Intent.CONFIRM_ORDER, cart_ready=True, cart_changed=False)
        == State.POS_INJECTION
    )


def test_confirm_with_changes_requires_reconfirmation():
    assert (
        next_state(State.CONFIRMING_PAYMENT, Intent.CONFIRM_ORDER, cart_ready=True, cart_changed=True)
        == State.CONFIRMING_PAYMENT
    )


def test_unready_cart_keeps_negotiating():
    assert (
        next_state(State.CONFIRMING_PAYMENT, Intent.CONFIRM_ORDER, cart_ready=False, cart_changed=True)
        == State.NEGOTIATING_MODIFICATIONS
    )
    assert (
        next_state(State.AWAITING_RESPONSE, Intent.QUESTION, cart_ready=True, cart_changed=False)
        == State.AWAITING_RESPONSE
    )


def test_decline_from_anywhere_active():
    for s in (State.AWAITING_RESPONSE, State.NEGOTIATING_MODIFICATIONS, State.CONFIRMING_PAYMENT):
        assert next_state(s, Intent.DECLINE, cart_ready=True, cart_changed=False) == State.DECLINED


def test_cannot_skip_to_pos_injection():
    with pytest.raises(InvalidTransition):
        assert_transition(State.NEGOTIATING_MODIFICATIONS, State.POS_INJECTION)
    with pytest.raises(InvalidTransition):
        assert_transition(State.COMPLETED, State.AWAITING_RESPONSE)
