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
# Heating
CONF_HEAT_KIND = "heat_kind"
CONF_AWAKE_MIN = "awake_min"
CONF_SLEEP_MIN = "sleep_min"
CONF_BASE_DAY_HEAT = "baseline_day_heat"
CONF_BASE_NIGHT_HEAT = "baseline_night_heat"
CONF_DEADBAND = "min_deadband"
CONF_AUTO_MODE = "auto_mode"
CONF_ELEC_PRICE = "electricity_price"
CONF_GAS_PRICE = "gas_price"  # per therm with US units, per kWh otherwise
CONF_FURNACE_SIZE = "furnace_size"  # BTU/h input with US units, kW otherwise
CONF_ELEC_HEAT_KW = "electric_heat_kw"
CONF_AUX_KW = "aux_kw"
CONF_AUX_BELOW = "aux_below"
CONF_SWITCH_BELOW = "switch_below"
# Presence
CONF_PEOPLE = "people"
CONF_TRAVEL = "travel_sensors"  # {person: travel time sensor in minutes}
CONF_DIRECTION = "direction_sensors"  # {person: proximity direction of travel sensor}
CONF_AWAY_MAX = "away_max"
CONF_AWAY_MIN = "away_min"

HEAT_KINDS = ["furnace", "electric", "heat_pump", "dual_fuel", "none"]
COOL_MODES = ("cool",)
MANAGED_MODES = ("cool", "heat", "heat_cool")

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
        CONF_HEAT_KIND: "furnace",
        CONF_AWAKE_MIN: s.awake_min,
        CONF_SLEEP_MIN: s.sleep_min,
        CONF_BASE_DAY_HEAT: s.heat_baseline[0][1],
        CONF_BASE_NIGHT_HEAT: s.heat_baseline[1][1],
        CONF_DEADBAND: s.min_deadband,
        CONF_AUTO_MODE: False,
        CONF_ELEC_PRICE: 0.15,
        CONF_GAS_PRICE: 1.30 if fahrenheit else 0.045,
        CONF_FURNACE_SIZE: 60000 if fahrenheit else 17.6,
        CONF_ELEC_HEAT_KW: 10.0,
        CONF_AUX_KW: 10.0,
        CONF_AUX_BELOW: s.aux_below,
        CONF_SWITCH_BELOW: s.switch_below,
        CONF_PEOPLE: [],
        CONF_TRAVEL: {},
        CONF_DIRECTION: {},
        CONF_AWAY_MAX: s.away_max,
        CONF_AWAY_MIN: s.away_min,
    }


def heat_ratios(o: dict[str, Any], fahrenheit: bool) -> dict[str, float]:
    """Heating cost per hour in AC hours, from the user's prices and equipment sizes."""
    elec = max(0.001, float(o[CONF_ELEC_PRICE]))
    ac_cost = max(0.1, float(o[CONF_AC_KW])) * elec
    gas_kw = float(o[CONF_FURNACE_SIZE]) / 3412.14 if fahrenheit else float(o[CONF_FURNACE_SIZE])
    gas_price_kwh = float(o[CONF_GAS_PRICE]) / 29.3071 if fahrenheit else float(o[CONF_GAS_PRICE])
    gas = gas_kw * gas_price_kwh / ac_cost
    kind = o[CONF_HEAT_KIND]
    if kind == "furnace":
        return {"heat_ratio": gas, "gas_ratio": gas, "aux_ratio": gas}
    if kind == "electric":
        r = float(o[CONF_ELEC_HEAT_KW]) * elec / ac_cost
        return {"heat_ratio": r, "gas_ratio": r, "aux_ratio": r}
    # A heat pump draws about what the AC does; the strips are priced on their own.
    aux = float(o[CONF_AUX_KW]) * elec / ac_cost
    return {"heat_ratio": 1.0, "gas_ratio": gas, "aux_ratio": aux}


def thermostat_modes(state) -> list[str]:
    return [str(m) for m in (state.attributes.get("hvac_modes") or [])] if state is not None else []


def run_mode(state) -> str | None:
    """Which side(s) this integration manages for the thermostat's current mode, or None (off, dry, fan)."""
    if state is None:
        return None
    if state.state in MANAGED_MODES:
        return state.state
    if state.state == "auto" and state.attributes.get("target_temp_high") is not None:
        return "heat_cool"  # some integrations call their two-setpoint mode auto
    return None


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


def setpoints_of(state) -> tuple[float | None, float | None]:
    """(cool setpoint, heat setpoint) a climate state is holding, None for a side its mode does not use."""
    mode = run_mode(state)

    def num(key):
        try:
            return float(state.attributes.get(key))
        except (TypeError, ValueError):
            return None

    if mode == "cool":
        return num("temperature"), None
    if mode == "heat":
        return None, num("temperature")
    if mode == "heat_cool":
        return num("target_temp_high"), num("target_temp_low")
    return None, None


def auto_switching(state, options: dict[str, Any]) -> bool:
    """The integration switches heat and cool itself: opted in, and the thermostat has no two-setpoint mode."""
    modes = thermostat_modes(state)
    return (
        bool(options.get(CONF_AUTO_MODE))
        and options.get(CONF_HEAT_KIND, "furnace") != "none"
        and "heat_cool" not in modes
        and "heat" in modes
        and "cool" in modes
        and run_mode(state) in ("cool", "heat")
    )


def plan_mode(state, options: dict[str, Any]) -> str | None:
    """What the optimizer plans for: the thermostat's mode, or both sides while auto switching."""
    if auto_switching(state, options):
        return "heat_cool"
    mode = run_mode(state)
    if mode in ("heat", "heat_cool") and options.get(CONF_HEAT_KIND, "furnace") == "none":
        return "cool" if mode == "heat_cool" else None
    return mode
