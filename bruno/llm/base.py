"""Provider-agnostic contract for the conversational model."""

from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from bruno.fsm import Intent, State
from bruno.menu import SIZES, CartItem, Menu


class AgentTurn(BaseModel):
    """Structured output the model must return for every customer message."""

    intent: Intent
    cart: list[CartItem] = Field(default_factory=list)  # full cart AFTER this message
    pickup_time: str | None = None  # local 24h "HH:MM"
    fulfillment: Literal["pickup", "delivery"] | None = None  # None = unchanged
    delivery_address: str | None = None
    remember: list[str] = Field(default_factory=list)  # durable facts about the customer
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

    def write(self, system: str, prompt: str) -> str:
        """Free-form text (used for outreach messages and nudges)."""
        ...


def turn_json_schema(menu: Menu) -> dict:
    """Strict JSON schema; item and modifier ids are enums so the model can only
    pick things that exist on the live menu."""
    # Nullable values are written as anyOf (Claude's structured outputs reject
    # an enum under a multi-type "type" list; OpenAI strict mode accepts anyOf too).
    nullable_str = {"anyOf": [{"type": "string"}, {"type": "null"}]}

    def nullable_enum(values: list[str]) -> dict:
        return {"anyOf": [{"type": "string", "enum": values}, {"type": "null"}]}

    topping_ids = [m.id for m in menu.modifiers.values() if m.group == "topping"] or list(menu.modifiers)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["intent", "cart", "pickup_time", "fulfillment", "delivery_address", "remember", "reply"],
        "properties": {
            "intent": {
                "type": "string",
                "enum": [i.value for i in Intent],
                "description": (
                    "update_order: accepted the usual or changed items/time/fulfillment. "
                    "confirm_order: explicit yes to place the order exactly as last recapped. "
                    "decline: not interested right now. question: asked something. "
                    "handoff: complaint, refund, allergy/medical question, or wants a person. other: anything else."
                ),
            },
            "cart": {
                "type": "array",
                "description": "The COMPLETE cart after applying this message (not a diff).",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["item_id", "size", "quantity", "modifiers", "halves", "notes"],
                    "properties": {
                        "item_id": {"type": "string", "enum": list(menu.items)},
                        "size": nullable_enum(list(SIZES)),
                        "quantity": {"type": "integer"},
                        "modifiers": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(menu.modifiers)},
                        },
                        "halves": {
                            "anyOf": [
                                {
                                    "type": "array",
                                    "items": {"type": "array", "items": {"type": "string", "enum": topping_ids}},
                                },
                                {"type": "null"},
                            ],
                            "description": "Half-and-half pizza: exactly two lists, [first half toppings, second half toppings]. Otherwise null.",
                        },
                        "notes": {**nullable_str, "description": "Kitchen note, e.g. 'well done'"},
                    },
                },
            },
            "pickup_time": {
                **nullable_str,
                "description": "Requested time, local 24h HH:MM, or null to keep the current one.",
            },
            "fulfillment": {
                **nullable_enum(["pickup", "delivery"]),
                "description": "Only set if the customer chose pickup or delivery in this message.",
            },
            "delivery_address": {**nullable_str, "description": "Full street address if they gave one."},
            "remember": {
                "type": "array",
                "items": {"type": "string"},
                "description": "New lasting preferences worth remembering for next time (e.g. 'likes pizza well done'). Usually empty.",
            },
            "reply": {
                "type": "string",
                "description": "What you text back. Use {order} {total} {when} {payment} placeholders instead of writing prices.",
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
