"""Runtime configuration, loaded from environment variables / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # --- Core infrastructure -------------------------------------------------
    database_url: str = "sqlite:///./bruno.db"  # Postgres/Supabase: postgresql+psycopg://...
    redis_url: str = "redis://localhost:6379/0"

    # --- Restaurant identity -------------------------------------------------
    restaurant_name: str = "Tony's"
    agent_name: str = "Marco"
    restaurant_timezone: str = "America/New_York"
    menu_path: str = "data/menu.json"
    tax_rate: float = 0.08
    min_prep_minutes: int = 25
    # Number customers can call to reach the voice agent. Leave blank when the
    # texting line and voice line are the same number ("call me right back").
    voice_phone_number: str = ""

    # --- LLM -----------------------------------------------------------------
    llm_provider: str = "auto"  # auto | openai | anthropic | mock
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5-5"
    llm_timeout_seconds: float = 20.0

    # --- Messaging (text / iMessage) ----------------------------------------
    messaging_provider: str = "console"  # console | sendblue
    sendblue_api_key_id: str | None = None
    sendblue_api_secret_key: str | None = None
    sendblue_from_number: str | None = None
    sendblue_base_url: str = "https://api.sendblue.co"
    sendblue_status_callback_url: str | None = None
    include_opt_out_footer: bool = True

    # Shared secret required on inbound webhooks (?secret=... or X-Bruno-Secret).
    webhook_secret: str | None = None

    # --- POS -----------------------------------------------------------------
    pos_provider: str = "console"  # console | square
    square_access_token: str | None = None
    square_location_id: str | None = None
    square_base_url: str = "https://connect.squareupsandbox.com"
    square_api_version: str = "2024-10-17"
    kitchen_ticket_log: str = "data/kitchen_tickets.jsonl"

    # --- Payments ------------------------------------------------------------
    payment_provider: str = "mock"  # mock

    # --- Cadence engine ------------------------------------------------------
    drift_multiplier: float = 1.25  # flag when days since last order > avg * this
    min_orders_for_cadence: int = 2
    min_drift_days: int = 5
    outreach_cooldown_days: int = 7
    outreach_scan_hour: int = 15  # 3:00 PM local: pick who to contact today
    outreach_send_time: str = "16:30"  # local time the texts actually go out
    conversation_ttl_hours: int = 12

    # --- Optional context ----------------------------------------------------
    weather_latitude: float | None = None
    weather_longitude: float | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
