"""Provider-agnostic contract for the conversational model."""

from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel, Field

from bruno.fsm import Intent, State
from bruno.menu import SIZES, CartItem, Menu


class AgentTurn(BaseModel):
    """Structured output the model must return for every customer message."""

    intent: Intent
    cart: list[CartItem] = Field(default_factory=list)  # full cart AFTER this message
    pickup_time: str | None = None  # local 24h "HH:MM"
    reply: str


@dataclass
class TurnContext:
    system_prompt: str
    history: list[dict]  # [{"role": "user" | "assistant", "content": str}], ends with the customer msg
    customer_message: str
    current_cart: list[CartItem]
    usual_order: list[CartItem]
    menu: Menu
    state: State
    pickup_time: str | None = None
    corrections: list[str] = field(default_factory=list)


class LLMError(Exception):
    pass


class LLMClient(Protocol):
    name: str

    def generate_turn(self, ctx: TurnContext) -> AgentTurn: ...


def turn_json_schema(menu: Menu) -> dict:
    """Strict JSON schema; item and modifier ids are enums so the model can only
    pick things that exist on the live menu."""
    nullable_str = {"type": ["string", "null"]}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["intent", "cart", "pickup_time", "reply"],
        "properties": {
            "intent": {
                "type": "string",
                "enum": [i.value for i in Intent],
                "description": (
                    "update_order: accepted the usual or changed items/time. "
                    "confirm_order: explicit yes to place the order as summarized. "
                    "decline: not interested right now. question: asked something. other: anything else."
                ),
            },
            "cart": {
                "type": "array",
                "description": "The COMPLETE cart after applying this message (not a diff).",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["item_id", "size", "quantity", "modifiers", "notes"],
                    "properties": {
                        "item_id": {"type": "string", "enum": list(menu.items)},
                        "size": {"type": ["string", "null"], "enum": [*SIZES, None]},
                        "quantity": {"type": "integer"},
                        "modifiers": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(menu.modifiers)},
                        },
                        "notes": nullable_str,
                    },
                },
            },
            "pickup_time": {
                **nullable_str,
                "description": "Requested pickup time, local 24h HH:MM, or null to keep the current one.",
            },
            "reply": {
                "type": "string",
                "description": "Short, casual text back to the customer. Never state prices or totals.",
            },
        },
    }


def messages_with_corrections(ctx: TurnContext) -> list[dict]:
    messages = list(ctx.history)
    if ctx.corrections:
        messages.append(
            {
                "role": "user",
                "content": "[SYSTEM: your last cart was rejected by the menu validator: "
                + "; ".join(ctx.corrections)
                + " Fix the cart, or ask the customer to clarify in your reply.]",
            }
        )
    return messages
