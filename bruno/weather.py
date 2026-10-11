"""Optional weather context (Open-Meteo, no API key). Never blocks a send."""

import logging
import time
from dataclasses import dataclass

import httpx

from bruno.config import Settings

log = logging.getLogger(__name__)

_cache: dict[tuple[float, float], tuple[float, "Weather"]] = {}
CACHE_SECONDS = 1800


@dataclass
class Weather:
    temperature_f: float
    is_rainy: bool
    is_snowy: bool

    @property
    def summary(self) -> str:
        cond = "snowing" if self.is_snowy else "raining" if self.is_rainy else "dry"
        return f"{round(self.temperature_f)}°F and {cond}"


def current_weather(settings: Settings) -> Weather | None:
    if settings.weather_latitude is None or settings.weather_longitude is None:
        return None
    key = (settings.weather_latitude, settings.weather_longitude)
    cached = _cache.get(key)
    if cached and time.time() - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        resp = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": key[0],
                "longitude": key[1],
                "current": "temperature_2m,precipitation,weather_code",
                "temperature_unit": "fahrenheit",
            },
            timeout=4,
        )
        resp.raise_for_status()
        cur = resp.json()["current"]
        code = int(cur.get("weather_code", 0))
        weather = Weather(
            temperature_f=float(cur["temperature_2m"]),
            is_rainy=cur.get("precipitation", 0) > 0 or 51 <= code <= 67 or 80 <= code <= 82 or code >= 95,
            is_snowy=71 <= code <= 77 or code in (85, 86),
        )
    except Exception as exc:  # weather is a nice-to-have
        log.warning("weather lookup failed: %s", exc)
        return None
    _cache[key] = (time.time(), weather)
    return weather
