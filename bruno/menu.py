"""Menu schema, cart validation and server-side pricing.

Prices are always computed here, never taken from the LLM.
"""

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

SIZES = ("small", "medium", "large")
MAX_QUANTITY = 20


class MenuItem(BaseModel):
    id: str
    name: str
    category: str
    description: str = ""
    price: int | None = None  # cents, for unsized items
    sizes: dict[str, int] | None = None  # cents per size
    modifier_groups: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)


class Modifier(BaseModel):
    id: str
    name: str
    group: str
    price: int | dict[str, int] = 0
    aliases: list[str] = Field(default_factory=list)

    def price_for(self, size: str | None) -> int:
        if isinstance(self.price, int):
            return self.price
        return self.price.get(size or "", 0)


class CartItem(BaseModel):
    """One requested line as it is stored on the conversation / produced by the LLM."""

    item_id: str
    size: str | None = None
    quantity: int = 1
    modifiers: list[str] = Field(default_factory=list)
    notes: str | None = None

    def key(self) -> tuple:
        return (self.item_id, self.size, self.quantity, tuple(sorted(self.modifiers)), self.notes or "")


class PricedModifier(BaseModel):
    id: str
    name: str
    price_cents: int


class PricedLine(BaseModel):
    item_id: str
    name: str
    size: str | None
    quantity: int
    modifiers: list[PricedModifier]
    notes: str | None
    unit_price_cents: int
    line_total_cents: int

    @property
    def display(self) -> str:
        size = f"{self.size.capitalize()} " if self.size else ""
        mods = ", ".join(m.name for m in self.modifiers)
        text = f"{self.quantity}x {size}{self.name}"
        if mods:
            text += f" w/ {mods}"
        if self.notes:
            text += f" ({self.notes})"
        return text


class ValidatedCart(BaseModel):
    lines: list[PricedLine] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    subtotal_cents: int = 0
    tax_cents: int = 0
    total_cents: int = 0

    @property
    def ready(self) -> bool:
        return bool(self.lines) and not self.errors

    def items(self) -> list[CartItem]:
        return [
            CartItem(
                item_id=line.item_id,
                size=line.size,
                quantity=line.quantity,
                modifiers=[m.id for m in line.modifiers],
                notes=line.notes,
            )
            for line in self.lines
        ]


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


class Menu:
    def __init__(self, data: dict, tax_rate: float):
        self.items: dict[str, MenuItem] = {i["id"]: MenuItem(**i) for i in data["items"]}
        self.modifiers: dict[str, Modifier] = {m["id"]: Modifier(**m) for m in data["modifiers"]}
        self.exclusive_groups: set[str] = set(data.get("exclusive_groups", []))
        self.tax_rate = tax_rate

    @classmethod
    def load(cls, path: str | Path, tax_rate: float) -> "Menu":
        return cls(json.loads(Path(path).read_text()), tax_rate)

    # ------------------------------------------------------------------ validation
    def validate(self, cart: list[CartItem] | list[dict]) -> ValidatedCart:
        result = ValidatedCart()
        for raw in cart:
            item = raw if isinstance(raw, CartItem) else CartItem(**raw)
            line = self._price_line(item, result.errors)
            if line:
                result.lines.append(line)
        result.subtotal_cents = sum(line.line_total_cents for line in result.lines)
        result.tax_cents = round(result.subtotal_cents * self.tax_rate)
        result.total_cents = result.subtotal_cents + result.tax_cents
        return result

    def _price_line(self, item: CartItem, errors: list[str]) -> PricedLine | None:
        menu_item = self.items.get(item.item_id)
        if not menu_item:
            errors.append(f"'{item.item_id}' isn't on the menu.")
            return None
        if not 1 <= item.quantity <= MAX_QUANTITY:
            errors.append(f"Quantity for {menu_item.name} must be between 1 and {MAX_QUANTITY}.")
            return None

        if menu_item.sizes:
            if item.size not in menu_item.sizes:
                errors.append(f"What size {menu_item.name}? ({'/'.join(menu_item.sizes)})")
                return None
            base = menu_item.sizes[item.size]
            size = item.size
        else:
            base = menu_item.price or 0
            size = None

        mods: list[PricedModifier] = []
        seen_groups: set[str] = set()
        for mod_id in dict.fromkeys(item.modifiers):  # de-dupe, keep order
            mod = self.modifiers.get(mod_id)
            if not mod or mod.group not in menu_item.modifier_groups:
                errors.append(f"{mod.name if mod else mod_id} isn't available on {menu_item.name}.")
                return None
            if mod.group in self.exclusive_groups and mod.group in seen_groups:
                errors.append(f"Only one {mod.group.replace('_', ' ')} choice for {menu_item.name}.")
                return None
            seen_groups.add(mod.group)
            mods.append(PricedModifier(id=mod.id, name=mod.name, price_cents=mod.price_for(size)))

        unit = base + sum(m.price_cents for m in mods)
        return PricedLine(
            item_id=menu_item.id,
            name=menu_item.name,
            size=size,
            quantity=item.quantity,
            modifiers=mods,
            notes=(item.notes or None),
            unit_price_cents=unit,
            line_total_cents=unit * item.quantity,
        )

    # ------------------------------------------------------------------ presentation
    def prompt_listing(self) -> str:
        """Compact menu for LLM / voice prompts (ids are what the model must output)."""
        out = ["ITEMS (id | name | price):"]
        for item in self.items.values():
            if item.sizes:
                price = ", ".join(f"{s} {money(c)}" for s, c in item.sizes.items())
            else:
                price = money(item.price or 0)
            groups = f" | mods: {', '.join(item.modifier_groups)}" if item.modifier_groups else ""
            desc = f" — {item.description}" if item.description else ""
            out.append(f"- {item.id} | {item.name}{desc} | {price}{groups}")
        out.append("MODIFIERS (id | name | group):")
        for mod in self.modifiers.values():
            out.append(f"- {mod.id} | {mod.name} | {mod.group}")
        return "\n".join(out)

    def describe(self, cart: list[CartItem] | list[dict]) -> str:
        """Short human description, e.g. 'large pepperoni pizza and Diet Coke'."""
        parts = []
        for line in self.validate(cart).lines:
            qty = f"{line.quantity} " if line.quantity > 1 else ""
            if self.items[line.item_id].category == "pizza":
                toppings = [m.name.lower() for m in line.modifiers if self.modifiers[m.id].group == "topping"]
                if line.item_id == "pizza":
                    flavor = " & ".join(toppings) or "cheese"
                else:
                    flavor = line.name.removesuffix(" Pizza").lower()
                    if toppings:
                        flavor += f" (+{', '.join(toppings)})"
                plural = "s" if line.quantity > 1 else ""
                parts.append(f"{qty}{line.size} {flavor} pizza{plural}")
            else:
                parts.append(f"{qty}{line.name.split(' (')[0]}")
        if len(parts) <= 1:
            return "".join(parts)
        return ", ".join(parts[:-1]) + " and " + parts[-1]


@lru_cache
def get_menu() -> Menu:
    from bruno.config import get_settings

    settings = get_settings()
    return Menu.load(settings.menu_path, settings.tax_rate)
