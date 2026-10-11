"""Inbound voice channel (Retell AI). The voice agent runs on Retell's
low-latency pipeline and calls back into Bruno for context and every cart /
checkout action, so it shares state with the text agent."""

from bruno.config import Settings
from bruno.menu import Menu, money
from bruno.orchestrator import CustomerContext, delivery_policy, order_summary, order_totals, voice_greeting
from bruno.timeutil import display_time


def dynamic_variables(ctx: CustomerContext, settings: Settings, menu: Menu) -> dict[str, str]:
    """Retell dynamic variables must all be strings."""
    v = ctx.validated
    fee = order_totals(v, ctx.fulfillment, settings).delivery_fee_cents
    return {
        "restaurant_name": settings.restaurant_name,
        "agent_name": settings.agent_name,
        "customer_name": ctx.customer.first_name if ctx.customer.name else "",
        "is_known_customer": "true" if ctx.customer.name else "false",
        "greeting": voice_greeting(ctx, settings),
        "usual_order": ctx.usual_desc,
        "current_cart": ctx.cart_desc,
        "current_order_summary": order_summary(v, ctx.pickup_time, ctx.payment_label, fee) if v.lines else "",
        "current_total": money(v.total_cents + fee) if v.lines else "",
        "remembered": "; ".join(ctx.customer.memory or []),
        "delivery_policy": delivery_policy(settings, bool(ctx.customer.payment_token)).replace("{total}", "the total"),
        "conversation_state": ctx.state.value if ctx.state else "none",
        "pickup_time": display_time(ctx.pickup_time) if ctx.pickup_time else "",
        "payment_method": ctx.payment_label,
        "local_time": ctx.local_now.strftime("%A %I:%M %p"),
        "menu": menu.prompt_listing(),
    }


VOICE_AGENT_PROMPT = """You are {{agent_name}}, the manager at {{restaurant_name}}, answering the phone.
Open with exactly: "{{greeting}}"

Caller: {{customer_name}} (known customer: {{is_known_customer}})
Their usual: {{usual_order}}
What we remember about them: {{remembered}}
Order already in progress from our texts: {{current_order_summary}}
Payment: {{payment_method}}. Local time: {{local_time}}.

How to take the order:
1. Work out what they want using ONLY the menu below. Never invent items or prices.
2. Every time the order changes, call update_cart with the COMPLETE order (menu ids, sizes, toppings as modifiers),
   plus fulfillment ("pickup" or "delivery") and delivery_address when relevant.
   {{delivery_policy}}
3. Read back the summary and total that update_cart returns, plus the pickup time, and ask "Should I put that in?"
4. Only after a clear yes, call place_order with customer_confirmed=true, then tell them what it returns.
Sound like a real person: short, warm, relaxed. Never quote a price that update_cart didn't give you.
If they sincerely ask whether you're a person, be honest that you're the restaurant's automated assistant.
For complaints, refunds or allergies, offer to have a team member call them back.

Menu:
{{menu}}
"""

_cart_items_schema = {
    "type": "array",
    "description": "The complete order",
    "items": {
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "description": "Menu item id"},
            "size": {"type": "string", "enum": ["small", "medium", "large"]},
            "quantity": {"type": "integer"},
            "modifiers": {"type": "array", "items": {"type": "string"}, "description": "Modifier ids"},
            "notes": {"type": "string"},
        },
        "required": ["item_id", "quantity"],
    },
}

RETELL_FUNCTIONS = [
    {
        "name": "update_cart",
        "description": "Replace the caller's cart with the complete order and get back a priced summary.",
        "parameters": {
            "type": "object",
            "properties": {
                "items": _cart_items_schema,
                "pickup_time": {"type": "string", "description": "Local 24h HH:MM, if the caller asked for a time"},
                "fulfillment": {"type": "string", "enum": ["pickup", "delivery"]},
                "delivery_address": {"type": "string", "description": "Full street address for delivery"},
            },
            "required": ["items"],
        },
    },
    {
        "name": "place_order",
        "description": "Charge and send the order to the kitchen. Only after the caller explicitly confirms the summary.",
        "parameters": {
            "type": "object",
            "properties": {"customer_confirmed": {"type": "boolean"}},
            "required": ["customer_confirmed"],
        },
    },
]
