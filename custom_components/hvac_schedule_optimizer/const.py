"""Constants for HVAC Schedule Optimizer."""

from __future__ import annotations

from typing import Any

from .engine import Settings

DOMAIN = "hvac_schedule_optimizer"

CONF_CLIMATE = "climate_entity"
CONF_INDOOR_SENSOR = "indoor_sensor"

CONF_AWAKE_START = "awake_start"
CONF_AWAKE_END = "awake_end"
CONF_AWAKE_MAX = "awake_max"
CONF_SLEEP_START = "sleep_start"
CONF_SLEEP_END = "sleep_end"
CONF_SLEEP_MAX = "sleep_max"
CONF_HYSTERESIS = "hysteresis"
CONF_BASE_WAKE_HOUR = "baseline_wake_hour"
CONF_BASE_DAY_TEMP = "baseline_day_temp"
CONF_BASE_SLEEP_HOUR = "baseline_sleep_hour"
CONF_BASE_NIGHT_TEMP = "baseline_night_temp"
CONF_RUN_TIME = "run_time"
CONF_HISTORY_DAYS = "history_days"

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
STORAGE_VERSION = 1


def default_options(fahrenheit: bool) -> dict[str, Any]:
    """Option defaults, derived from the engine so there is one source of truth."""
    s = Settings.defaults(fahrenheit)
    return {
        CONF_AWAKE_START: s.awake_start,
        CONF_AWAKE_END: s.awake_end,
        CONF_AWAKE_MAX: s.awake_max,
        CONF_SLEEP_START: s.sleep_start,
        CONF_SLEEP_END: s.sleep_end,
        CONF_SLEEP_MAX: s.sleep_max,
        CONF_HYSTERESIS: s.hysteresis,
        CONF_BASE_WAKE_HOUR: s.baseline[0][0] // 60,
        CONF_BASE_DAY_TEMP: s.baseline[0][1],
        CONF_BASE_SLEEP_HOUR: s.baseline[1][0] // 60,
        CONF_BASE_NIGHT_TEMP: s.baseline[1][1],
        CONF_RUN_TIME: "04:30:00",
        CONF_HISTORY_DAYS: 10,
    }
