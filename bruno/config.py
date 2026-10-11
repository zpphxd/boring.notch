"""Configuration.

Values come from environment variables / .env, and anything the owner saves in
the dashboard's Settings page (stored in the database) overrides them. So the
only thing a deployment strictly needs in the environment is DATABASE_URL.
"""

from dataclasses import dataclass
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # --- Core infrastructure (environment only) ------------------------------
    database_url: str = "sqlite:///./bruno.db"  # Postgres/Supabase: postgresql+psycopg://...
    secret_key: str | None = None  # signs dashboard sessions; auto-generated if unset
    admin_password: str | None = None  # dashboard login; can also be created on first visit
    public_base_url: str = ""  # e.g. https://bruno.yourpizzeria.com (for webhook URLs)
    scheduler_enabled: bool = True

    # --- Restaurant -----------------------------------------------------------
    restaurant_name: str = "Tony's"
    agent_name: str = "Marco"
    restaurant_timezone: str = "America/New_York"
    restaurant_address: str = ""
    restaurant_phone: str = ""
    owner_phone: str = ""  # gets a text when a customer needs a human
    menu_path: str = "data/menu.json"
    menu_json: str | None = None  # dashboard-edited menu overrides the file
    tax_rate: float = 0.08
    min_prep_minutes: int = 25
    opening_time: str = "11:00"
    closing_time: str = "22:00"
    voice_phone_number: str = ""  # blank = customers can call the texting number

    # --- LLM ------------------------------------------------------------------
    llm_provider: str = "auto"  # auto | openai | anthropic | mock
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5-5"
    llm_timeout_seconds: float = 20.0

    # --- Texting --------------------------------------------------------------
    messaging_provider: str = "auto"  # auto | console | sendblue
    sendblue_api_key_id: str | None = None
    sendblue_api_secret_key: str | None = None
    sendblue_from_number: str | None = None
    sendblue_base_url: str = "https://api.sendblue.co"
    sendblue_status_callback_url: str | None = None
    webhook_secret: str | None = None  # ?secret=... or X-Bruno-Secret on every webhook
    human_delays: bool = True  # typing indicator + realistic pauses before replies

    # --- POS ------------------------------------------------------------------
    pos_provider: str = "auto"  # auto | console | square
    square_access_token: str | None = None
    square_location_id: str | None = None
    square_base_url: str = "https://connect.squareup.com"
    square_api_version: str = "2024-10-17"
    kitchen_ticket_log: str = "data/kitchen_tickets.jsonl"

    # --- Payments -------------------------------------------------------------
    payment_provider: str = "auto"  # auto | stripe | mock
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    require_prepay: bool = False  # pickup orders without a saved card get a pay link too

    # --- Delivery -------------------------------------------------------------
    doordash_developer_id: str | None = None
    doordash_key_id: str | None = None
    doordash_signing_secret: str | None = None
    doordash_base_url: str = "https://openapi.doordash.com"
    delivery_fee: float = 4.99  # charged to the customer for DoorDash Drive deliveries
    delivery_min_order: float = 15.0
    doordash_store_url: str = ""  # marketplace page, used when Drive isn't set up
    grubhub_store_url: str = ""

    # --- Outreach autopilot ---------------------------------------------------
    autopilot_enabled: bool = False
    outreach_mode: str = "approve"  # approve (owner OKs each text) | auto
    daily_outreach_cap: int = 25
    send_window_start: str = "11:00"
    send_window_end: str = "19:30"
    attribution_hours: int = 48
    drift_multiplier: float = 1.25
    min_orders_for_cadence: int = 2
    min_drift_days: int = 5
    outreach_cooldown_days: int = 7
    conversation_ttl_hours: int = 12
    nudge_after_minutes: int = 25

    # --- Optional context -----------------------------------------------------
    weather_latitude: float | None = None
    weather_longitude: float | None = None

    # --- Derived --------------------------------------------------------------
    @property
    def llm_ready(self) -> bool:
        return bool(self.openai_api_key or self.anthropic_api_key) or self.llm_provider == "mock"

    @property
    def sendblue_ready(self) -> bool:
        return bool(self.sendblue_api_key_id and self.sendblue_api_secret_key)

    @property
    def drive_ready(self) -> bool:
        return bool(
            self.doordash_developer_id
            and self.doordash_key_id
            and self.doordash_signing_secret
            and self.restaurant_address
        )

    @property
    def delivery_fee_cents(self) -> int:
        return round(self.delivery_fee * 100)


ENV_ONLY = {"database_url", "secret_key", "scheduler_enabled"}


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    group: str
    secret: bool = False
    help: str = ""
    kind: str = "text"  # text | bool | select | number | textarea
    options: tuple[str, ...] = ()


FIELDS: list[Field] = [
    Field("restaurant_name", "Restaurant name", "Restaurant"),
    Field("agent_name", "Who's texting (first name)", "Restaurant", help="Customers see texts from this person."),
    Field("restaurant_timezone", "Timezone", "Restaurant", help="e.g. America/New_York"),
    Field("public_base_url", "This app's public URL", "Restaurant", help="e.g. https://bruno.tonyspizza.com (webhooks + payment links)"),
    Field("restaurant_address", "Pickup address", "Restaurant", help="Required for DoorDash Drive deliveries."),
    Field("restaurant_phone", "Store phone", "Restaurant"),
    Field("owner_phone", "Owner / manager cell", "Restaurant", help="Texted when a customer needs a human."),
    Field("tax_rate", "Sales tax rate", "Restaurant", kind="number", help="0.08 = 8%"),
    Field("min_prep_minutes", "Minimum prep time (min)", "Restaurant", kind="number"),
    Field("opening_time", "Kitchen opens (HH:MM)", "Restaurant"),
    Field("closing_time", "Kitchen closes (HH:MM)", "Restaurant"),
    Field("openai_api_key", "OpenAI API key", "AI model", secret=True, help="GPT-4o. Either key works."),
    Field("openai_model", "OpenAI model", "AI model"),
    Field("anthropic_api_key", "Anthropic API key", "AI model", secret=True),
    Field("anthropic_model", "Anthropic model", "AI model"),
    Field("sendblue_api_key_id", "Sendblue API key ID", "Texting (Sendblue)", secret=True),
    Field("sendblue_api_secret_key", "Sendblue API secret", "Texting (Sendblue)", secret=True),
    Field("sendblue_from_number", "Sendblue line (from number)", "Texting (Sendblue)", help="+1..., your Sendblue number"),
    Field("webhook_secret", "Webhook secret", "Texting (Sendblue)", secret=True, help="Appended to webhook URLs as ?secret="),
    Field("human_delays", "Typing indicator + human pauses", "Texting (Sendblue)", kind="bool"),
    Field("voice_phone_number", "Voice line (Retell number)", "Voice (Retell)", help="Blank if it's the same as the texting number."),
    Field("square_access_token", "Square access token", "POS (Square)", secret=True),
    Field("square_location_id", "Square location ID", "POS (Square)"),
    Field("square_base_url", "Square API base", "POS (Square)", help="Sandbox: https://connect.squareupsandbox.com"),
    Field("stripe_secret_key", "Stripe secret key", "Payments (Stripe)", secret=True,
          help="Saved cards for one-text reorders + secure pay links. Blank = pay at pickup."),
    Field("stripe_webhook_secret", "Stripe webhook signing secret", "Payments (Stripe)", secret=True),
    Field("require_prepay", "Require payment up front for pickup too", "Payments (Stripe)", kind="bool"),
    Field("doordash_developer_id", "DoorDash Drive developer ID", "Delivery", secret=True),
    Field("doordash_key_id", "DoorDash Drive key ID", "Delivery", secret=True),
    Field("doordash_signing_secret", "DoorDash Drive signing secret", "Delivery", secret=True),
    Field("delivery_fee", "Delivery fee charged ($)", "Delivery", kind="number"),
    Field("delivery_min_order", "Delivery minimum ($)", "Delivery", kind="number"),
    Field("doordash_store_url", "Your DoorDash store link", "Delivery", help="Sent when a customer wants delivery and Drive isn't set up."),
    Field("grubhub_store_url", "Your Grubhub store link", "Delivery"),
    Field("autopilot_enabled", "Autopilot outreach", "Autopilot", kind="bool"),
    Field("outreach_mode", "Mode", "Autopilot", kind="select", options=("approve", "auto"),
          help="approve: you OK each text in the Outreach tab. auto: Bruno sends on its own."),
    Field("daily_outreach_cap", "Max outreach texts per day", "Autopilot", kind="number"),
    Field("send_window_start", "Send window start (HH:MM)", "Autopilot"),
    Field("send_window_end", "Send window end (HH:MM)", "Autopilot"),
    Field("outreach_cooldown_days", "Min days between texts to one person", "Autopilot", kind="number"),
    Field("weather_latitude", "Weather latitude", "Autopilot", kind="number", help="Optional: weather-aware messages"),
    Field("weather_longitude", "Weather longitude", "Autopilot", kind="number"),
]
FIELD_KEYS = {f.key for f in FIELDS} | {"menu_json"}


@lru_cache
def env_settings() -> Settings:
    return Settings()


def merge(base: Settings, overrides: dict[str, str]) -> Settings:
    clean = {k: v for k, v in overrides.items() if k in FIELD_KEYS and k not in ENV_ONLY and v not in (None, "")}
    data = {**base.model_dump(), **clean}
    return Settings.model_validate(data)


@lru_cache
def get_settings() -> Settings:
    """Environment settings overlaid with dashboard-saved values."""
    from bruno.configstore import read_overrides

    base = env_settings()
    try:
        return merge(base, read_overrides())
    except Exception:  # DB not ready yet (first boot) or a bad value: fall back to env
        return base


def reload_settings() -> None:
    get_settings.cache_clear()
