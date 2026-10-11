"""Menu schema, cart validation and server-side pricing.

Prices are always computed here, never taken from the LLM.
"""

import difflib
import json
import re
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

SIZES = ("small", "medium", "large")
SIZE_WORDS = {
    "small": "small", "sm": "small", "personal": "small", '10"': "small", "10 inch": "small",
    "medium": "medium", "med": "medium", "md": "medium", '12"': "medium", "12 inch": "medium",
    "large": "large", "lg": "large", "lrg": "large", "xl": "large", '14"': "large", "14 inch": "large",
    '16"': "large", "16 inch": "large",
}
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
    modifiers: list[str] = Field(default_factory=list)  # whole item
    halves: list[list[str]] | None = None  # pizzas only: [first-half toppings, second-half toppings]
    notes: str | None = None

    def key(self) -> tuple:
        halves = tuple(tuple(sorted(h)) for h in self.halves) if self.halves else ()
        return (self.item_id, self.size, self.quantity, tuple(sorted(self.modifiers)), halves, self.notes or "")


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
    halves: list[list[PricedModifier]] | None = None
    notes: str | None
    unit_price_cents: int
    line_total_cents: int

    @property
    def halves_text(self) -> str:
        if not self.halves:
            return ""
        return " / ".join("½ " + (", ".join(m.name for m in h) or "Cheese") for h in self.halves)

    @property
    def display(self) -> str:
        size = f"{self.size.capitalize()} " if self.size else ""
        mods = ", ".join(m.name for m in self.modifiers)
        text = f"{self.quantity}x {size}{self.name}"
        if mods:
            text += f" w/ {mods}"
        if self.halves:
            text += f" ({self.halves_text})"
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
                halves=[[m.id for m in h] for h in line.halves] if line.halves else None,
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
        self.house_rules: list[str] = list(data.get("house_rules", []))
        self.tax_rate = tax_rate

        self._aliases = sorted(
            [(a.lower(), "item", i.id) for i in self.items.values() for a in [*i.aliases, i.name]]
            + [(a.lower(), "mod", m.id) for m in self.modifiers.values() for a in [*m.aliases, m.name]],
            key=lambda t: -len(t[0]),
        )

    @classmethod
    def load(cls, path: str | Path, tax_rate: float) -> "Menu":
        return cls(json.loads(Path(path).read_text()), tax_rate)

    # ------------------------------------------------------------------ text matching
    def find(self, text: str, kind: str | None = None) -> tuple[str, str] | None:
        """Longest alias mentioned in `text` -> ("item"|"mod", id)."""
        text = text.lower()
        for alias, k, ident in self._aliases:
            if (kind is None or k == kind) and re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", text):
                return k, ident
        return None

    def parse_item_text(self, text: str, quantity: int = 1) -> CartItem | None:
        """Map a free-text line from a POS / DoorDash / spreadsheet export to a cart item,
        e.g. 'Lg Pepperoni Pizza' -> pizza(large, [pepperoni])."""
        raw = text.lower().strip()
        if not raw:
            return None
        size = next(
            (v for k, v in sorted(SIZE_WORDS.items(), key=lambda kv: -len(kv[0]))
             if re.search(rf"(?<!\w){re.escape(k)}(?!\w)", raw)),
            None,
        )
        hit = self.find(raw, "item")
        item_id = hit[1] if hit else None
        mods = []
        remaining = raw
        for alias, k, ident in self._aliases:
            if k == "mod" and re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", remaining):
                mods.append(ident)
                remaining = re.sub(rf"(?<!\w){re.escape(alias)}(?!\w)", " ", remaining)
        if not item_id and mods and any(self.modifiers[m].group == "topping" for m in mods):
            item_id = "pizza"
        if not item_id:
            names = {a: ident for a, k, ident in self._aliases if k == "item"}
            close = difflib.get_close_matches(raw, list(names), n=1, cutoff=0.75)
            if not close:
                return None
            item_id = names[close[0]]
        item = self.items[item_id]
        allowed = [m for m in dict.fromkeys(mods) if self.modifiers[m].group in item.modifier_groups]
        if item.sizes:
            size = size if size in item.sizes else "large"
        else:
            size = None
        return CartItem(item_id=item_id, size=size, quantity=max(1, quantity), modifiers=allowed)

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

        halves: list[list[PricedModifier]] | None = None
        if item.halves:
            if menu_item.category != "pizza" or len(item.halves) != 2:
                errors.append(f"Half-and-half only works on pizzas (two halves) for {menu_item.name}.")
                return None
            halves = []
            for half in item.halves:
                priced = []
                for mod_id in dict.fromkeys(half):
                    mod = self.modifiers.get(mod_id)
                    if not mod or mod.group != "topping" or "topping" not in menu_item.modifier_groups:
                        errors.append(f"{mod.name if mod else mod_id} can't go on half a {menu_item.name}.")
                        return None
                    # half a topping costs half (rounded up to the cent)
                    priced.append(PricedModifier(id=mod.id, name=mod.name, price_cents=-(-mod.price_for(size) // 2)))
                halves.append(priced)
            if not any(halves):
                halves = None

        unit = base + sum(m.price_cents for m in mods) + sum(m.price_cents for h in (halves or []) for m in h)
        return PricedLine(
            item_id=menu_item.id,
            name=menu_item.name,
            size=size,
            quantity=item.quantity,
            modifiers=mods,
            halves=halves,
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
        if self.house_rules:
            out.append("HOUSE RULES:")
            out += [f"- {rule}" for rule in self.house_rules]
        return "\n".join(out)

    def describe(self, cart: list[CartItem] | list[dict]) -> str:
        """Short human description, e.g. 'large pepperoni pizza and Diet Coke'."""
        parts = []
        lines = sorted(self.validate(cart).lines, key=lambda ln: self.items[ln.item_id].category != "pizza")
        for line in lines:
            qty = f"{line.quantity} " if line.quantity > 1 else ""
            if self.items[line.item_id].category == "pizza":
                toppings = [m.name.lower() for m in line.modifiers if self.modifiers[m.id].group == "topping"]
                if line.halves:
                    sides = [_half_name(h) for h in line.halves]
                    flavor = f"half {sides[0]} / half {sides[1]}" + (f" (+{', '.join(toppings)})" if toppings else "")
                elif line.item_id == "pizza":
                    flavor = " & ".join(toppings) or "cheese"
                else:
                    flavor = line.name.removesuffix(" Pizza").lower()
                    if toppings:
                        flavor += f" (+{', '.join(toppings)})"
                plural = "s" if line.quantity > 1 else ""
                parts.append(f"{qty}{line.size} {flavor} pizza{plural}")
            else:
                name = line.name.split(" (")[0]
                if line.quantity > 1 and not name.endswith(("s", "i")):
                    name += "s"
                parts.append(f"{qty}{name}")
        if len(parts) <= 1:
            return "".join(parts)
        return ", ".join(parts[:-1]) + " and " + parts[-1]


VEGGIE = {"mushroom", "onion", "green_pepper", "black_olive", "jalapeno"}


def _half_name(half: list[PricedModifier]) -> str:
    if not half:
        return "cheese"
    if len(half) > 2:
        return "veggie" if all(m.id in VEGGIE for m in half) else f"{len(half)}-topping"
    return " & ".join(m.name.lower() for m in half)


@lru_cache
def get_menu() -> Menu:
    from bruno.config import get_settings

    settings = get_settings()
    if settings.menu_json:
        return Menu(json.loads(settings.menu_json), settings.tax_rate)
    return Menu.load(settings.menu_path, settings.tax_rate)
