"""weather.py — Compatibility shim. Do not add logic here.

All forecast fetching has moved to weathercore/weather_v2.py.
This file re-exports the symbols that existing callers need so their
imports continue to work without modification.

Callers served by this shim:
  aviation_weather.py  : celsius_to_fahrenheit
  strategy.py          : celsius_to_fahrenheit, fahrenheit_to_celsius
  observed_temps.py    : CITIES, celsius_to_fahrenheit, get_primary_icao
"""

# Re-export everything active callers need from the single source of truth
from weather_v2 import (  # noqa: F401
    CITIES,
    celsius_to_fahrenheit,
    fahrenheit_to_celsius,
    get_forecast,
    get_forecast_fahrenheit,
    WeatherFetchError,
)

# ---------------------------------------------------------------------------
# AVIATION_ICAO — migrated to aviation_weather.py (its true home).
# Backward-compat shim: import from aviation_weather so existing callers
# (observed_temps.py, get_primary_icao) still work.
# ---------------------------------------------------------------------------
from aviation_weather import AVIATION_ICAO  # noqa: F401 - re-export

AVIATION_ICAO  # suppress unused warning


def get_primary_icao(city: str) -> str | None:
    """Returns the primary ICAO station code for a city, or None if unmapped."""
    return AVIATION_ICAO.get(city)
