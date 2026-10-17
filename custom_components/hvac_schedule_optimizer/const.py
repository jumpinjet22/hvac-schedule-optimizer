"""Constants for HVAC Schedule Optimizer."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.climate import ClimateEntityFeature

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
CONF_ROOM_SENSORS = "room_sensors"
CONF_ROOM_WHEN = "room_when"
CONF_OUTDOOR_SENSORS = "outdoor_sensors"
CONF_FAN_CONTROL = "fan_control"
CONF_FAN_MODE_NORMAL = "fan_mode_normal"
CONF_FAN_MODE_CIRCULATE = "fan_mode_circulate"
CONF_FAN_WATTS = "fan_watts"
CONF_AC_KW = "ac_kw"
CONF_FAN_HUMIDITY_MAX = "fan_humidity_max"
CONF_HUMIDITY_SENSOR = "humidity_sensor"

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
        CONF_ROOM_SENSORS: [],
        CONF_ROOM_WHEN: {},
        CONF_OUTDOOR_SENSORS: [],
        CONF_FAN_CONTROL: False,
        CONF_FAN_WATTS: 400,
        CONF_AC_KW: 3.5,
        CONF_FAN_HUMIDITY_MAX: 60,
    }


def thermostat_fan_modes(state) -> list[str]:
    """The fan modes a climate entity reports, or [] when it has no fan control."""
    if state is None:
        return []
    if not int(state.attributes.get("supported_features") or 0) & ClimateEntityFeature.FAN_MODE:
        return []
    modes = state.attributes.get("fan_modes") or []
    return [str(m) for m in modes]


def guess_fan_modes(modes: list[str]) -> tuple[str | None, str | None]:
    """Best guess at (normal, circulate) among a thermostat's own mode names."""
    words = [set(re.split(r"[\s_-]+", m.lower())) for m in modes]

    def first(*keys: str) -> str | None:
        for key in keys:
            for m, w in zip(modes, words):
                if set(key.split()) <= w:
                    return m
        return None

    normal = first("auto", "automatic") or (modes[0] if modes else None)
    circ = first("circulate", "circulation", "diffuse", "on", "low")
    if circ == normal:
        circ = next((m for m in modes if m != normal), None)
    return normal, circ
