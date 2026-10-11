"""Dashboard-saved settings, admin password and session secret, kept in the DB."""

import hashlib
import hmac
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from bruno.config import reload_settings
from bruno.models import ConfigEntry

_INTERNAL = {"_admin_password_hash", "_session_secret"}


def read_overrides(session: Session | None = None) -> dict[str, str]:
    if session is None:
        from bruno.db import session_scope

        with session_scope() as s:
            return read_overrides(s)
    return {e.key: e.value for e in session.scalars(select(ConfigEntry)) if e.key not in _INTERNAL}


def get_value(session: Session, key: str) -> str | None:
    entry = session.get(ConfigEntry, key)
    return entry.value if entry else None


def set_values(session: Session, values: dict[str, str | None]) -> None:
    """Save (or clear, with None/"") settings, then reload runtime providers."""
    for key, value in values.items():
        entry = session.get(ConfigEntry, key)
        if value in (None, ""):
            if entry:
                session.delete(entry)
        elif entry:
            entry.value = str(value)
        else:
            session.add(ConfigEntry(key=key, value=str(value)))
    session.flush()
    reload_runtime()


def reload_runtime() -> None:
    reload_settings()
    from bruno.deps import get_deps
    from bruno.menu import get_menu

    get_deps.cache_clear()
    get_menu.cache_clear()


# ------------------------------------------------------------------ admin auth
def _hash(password: str, salt: bytes) -> str:
    return salt.hex() + ":" + hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()


def set_admin_password(session: Session, password: str) -> None:
    set_values(session, {"_admin_password_hash": _hash(password, secrets.token_bytes(16))})


def has_admin_password(session: Session, env_password: str | None) -> bool:
    return bool(env_password) or get_value(session, "_admin_password_hash") is not None


def check_admin_password(session: Session, password: str, env_password: str | None) -> bool:
    if env_password:
        return hmac.compare_digest(password, env_password)
    stored = get_value(session, "_admin_password_hash")
    if not stored:
        return False
    salt_hex, _ = stored.split(":", 1)
    return hmac.compare_digest(_hash(password, bytes.fromhex(salt_hex)), stored)


def session_secret(session: Session, env_secret: str | None) -> str:
    if env_secret:
        return env_secret
    existing = get_value(session, "_session_secret")
    if existing:
        return existing
    value = secrets.token_urlsafe(32)
    session.add(ConfigEntry(key="_session_secret", value=value))
    session.flush()
    return value
