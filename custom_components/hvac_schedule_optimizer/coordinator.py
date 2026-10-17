"""Coordinator: pulls history and weather, runs the optimizer, hands results to the scheduler."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from functools import partial
from typing import Any

import aiohttp
from homeassistant.components.recorder import get_instance, history
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from . import engine
from .const import (
    CONF_AWAKE_END,
    CONF_AWAKE_MAX,
    CONF_AWAKE_START,
    CONF_BASE_DAY_TEMP,
    CONF_BASE_NIGHT_TEMP,
    CONF_BASE_SLEEP_HOUR,
    CONF_BASE_WAKE_HOUR,
    CONF_CLIMATE,
    CONF_HISTORY_DAYS,
    CONF_HYSTERESIS,
    CONF_INDOOR_SENSOR,
    CONF_OUTDOOR_SENSORS,
    CONF_ROOM_SENSORS,
    CONF_ROOM_WHEN,
    CONF_SLEEP_END,
    CONF_SLEEP_MAX,
    CONF_SLEEP_START,
    DOMAIN,
    OPEN_METEO_URL,
    default_options,
)
from .scheduler import Scheduler

_LOGGER = logging.getLogger(__name__)


def _num(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


_TEMP_SUFFIX = re.compile(r"[\s_-]*\b(temperature|temp)\b\s*$", re.IGNORECASE)
_GENERIC = {"", "air", "ambient", "room", "indoor", "inside", "sensor", "current"}


def _strip_temp(name: str | None) -> str:
    return _TEMP_SUFFIX.sub("", (name or "").strip()).strip()


def _dedupe_words(name: str) -> str:
    """'Master Bedroom Master Bedroom' -> 'Master Bedroom' (drops an immediately repeated run of words)."""
    words = name.split()
    i = 0
    while i < len(words):
        for k in range(len(words[i:]) // 2, 0, -1):
            if [w.lower() for w in words[i : i + k]] == [w.lower() for w in words[i + k : i + 2 * k]]:
                del words[i + k : i + 2 * k]
                break
        else:
            i += 1
    return " ".join(words)


def room_name(hass: HomeAssistant, entity_id: str) -> str:
    """Short label for a room sensor: the sensor's own name without 'Temperature', else its device's name."""
    st = hass.states.get(entity_id)
    full = st.name if st else entity_id
    ent = er.async_get(hass).async_get(entity_id)
    if ent and ent.has_entity_name and ent.device_id:
        dev = dr.async_get(hass).async_get(ent.device_id)
        own = _strip_temp(ent.name or ent.original_name)
        if own.lower() not in _GENERIC:
            return own
        if dev and (dev_name := _strip_temp(dev.name_by_user or dev.name)):
            return dev_name
    return _strip_temp(_dedupe_words(full)) or full


def baseline_nodes(hass: HomeAssistant, options: dict[str, Any]) -> list[dict]:
    """The schedule the user runs today, as set in the options."""
    f = hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT
    o = {**default_options(f), **options}
    return [
        {"time": engine.hhmm(int(o[CONF_BASE_WAKE_HOUR]) * 60), "temp": float(o[CONF_BASE_DAY_TEMP])},
        {"time": engine.hhmm(int(o[CONF_BASE_SLEEP_HOUR]) * 60), "temp": float(o[CONF_BASE_NIGHT_TEMP])},
    ]


class OptimizerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Runs the optimizer on demand (button) and on a daily timer."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, scheduler: Scheduler) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry, update_interval=None)
        self.scheduler = scheduler

    @property
    def climate_entity(self) -> str:
        return self.config_entry.data[CONF_CLIMATE]

    # ---------------------------------------------------------- settings
    def _settings(self, climate_state) -> engine.Settings:
        f = self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT
        o = {**default_options(f), **self.config_entry.options}
        s = engine.Settings.defaults(f)
        s.awake_start = int(o[CONF_AWAKE_START])
        s.awake_end = int(o[CONF_AWAKE_END])
        s.awake_max = float(o[CONF_AWAKE_MAX])
        s.sleep_start = int(o[CONF_SLEEP_START])
        s.sleep_end = int(o[CONF_SLEEP_END])
        s.sleep_max = float(o[CONF_SLEEP_MAX])
        s.hysteresis = float(o[CONF_HYSTERESIS])
        s.baseline = [
            (int(o[CONF_BASE_WAKE_HOUR]) * 60, float(o[CONF_BASE_DAY_TEMP])),
            (int(o[CONF_BASE_SLEEP_HOUR]) * 60, float(o[CONF_BASE_NIGHT_TEMP])),
        ]
        step = _num(climate_state.attributes.get("target_temp_step"))
        if step and step > 0:
            s.step = step
        return s

    def _history_days(self) -> int:
        f = self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT
        o = {**default_options(f), **self.config_entry.options}
        return max(3, min(30, int(o[CONF_HISTORY_DAYS])))

    # ---------------------------------------------------------- update
    async def _async_update_data(self) -> dict[str, Any]:
        try:
            result = await self._compute()
        except Exception as err:  # noqa: BLE001 - surface any failure as a status instead of failing setup
            _LOGGER.warning("Optimizer run failed: %s", err)
            result = {
                "status": "error",
                "message": str(err),
                "ok_to_apply": False,
                "validated": False,
                "best": None,
                "baseline": None,
                "model": None,
                "validation": None,
                "energy_change_pct": None,
            }
        result["generated_at"] = dt_util.utcnow().isoformat()
        return result

    async def _compute(self) -> dict[str, Any]:
        state = self.hass.states.get(self.climate_entity)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            raise HomeAssistantError(f"{self.climate_entity} is unavailable")
        if state.state != "cool":
            raise HomeAssistantError(
                f"{self.climate_entity} is in '{state.state}' mode. This version only optimizes cooling."
            )
        settings = self._settings(state)
        days = self._history_days()
        sensor = self.config_entry.data.get(CONF_INDOOR_SENSOR)

        now = int(dt_util.utcnow().timestamp())
        t0 = now - now % engine.STEP_S
        n_hist = days * 96
        n_hor = 24 * 4
        g0 = t0 - n_hist * engine.STEP_S

        recorder = get_instance(self.hass)
        if not await recorder.async_db_ready:
            raise HomeAssistantError("The recorder database is not ready")

        o = {**default_options(settings.fahrenheit), **self.config_entry.options}
        room_ids = [r for r in o[CONF_ROOM_SENSORS] if r != sensor]
        outdoor_ids = list(o[CONF_OUTDOOR_SENSORS])
        ids = list(dict.fromkeys([self.climate_entity] + ([sensor] if sensor else []) + room_ids + outdoor_ids))
        states = await recorder.async_add_executor_job(
            partial(
                history.get_significant_states,
                self.hass,
                dt_util.utc_from_timestamp(g0),
                dt_util.utc_from_timestamp(now),
                entity_ids=ids,
                include_start_time_state=True,
                significant_changes_only=False,
                minimal_response=False,
                no_attributes=False,
            )
        )

        indoor, on, setp = [], [], []
        for st in states.get(self.climate_entity, []):
            ts = st.last_updated.timestamp()
            if st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                indoor.append((ts, None))
                on.append((ts, None))
                setp.append((ts, None))
                continue
            a = st.attributes
            if not sensor:
                indoor.append((ts, _num(a.get("current_temperature"))))
            action = a.get("hvac_action")
            on.append((ts, None if action in (None, "heating") else (1.0 if action == "cooling" else 0.0)))
            setp.append((ts, _num(a.get("temperature"))))
        if sensor:
            indoor = self._sensor_points(states.get(sensor, []))
        for series in (indoor, on, setp):
            series.sort(key=lambda p: p[0])

        wts, wtemp, wsun = await self._fetch_weather(days)

        local_minutes = []
        for i in range(n_hor):
            d = dt_util.as_local(dt_util.utc_from_timestamp(t0 + i * engine.STEP_S))
            local_minutes.append(d.hour * 60 + d.minute)

        local_minutes_past = []
        for i in range(-96, 0):
            d = dt_util.as_local(dt_util.utc_from_timestamp(t0 + i * engine.STEP_S))
            local_minutes_past.append(d.hour * 60 + d.minute)

        inputs = engine.Inputs(
            g0=g0,
            n_hist=n_hist,
            n_hor=n_hor,
            data_end=now,
            indoor=indoor,
            on=on,
            setpoint=setp,
            weather_ts=wts,
            weather_temp=wtemp,
            weather_sun=wsun,
            local_minutes=local_minutes,
            settings=settings,
            local_minutes_past=local_minutes_past,
            active_nodes=list(self.scheduler.nodes) if self.scheduler.nodes else None,
            rooms=[
                {
                    "id": r,
                    "name": room_name(self.hass, r),
                    "when": o[CONF_ROOM_WHEN].get(r, "always"),
                    "points": self._sensor_points(states.get(r, [])),
                }
                for r in room_ids
            ],
            outdoor=[p for r in outdoor_ids if (p := self._sensor_points(states.get(r, [])))],
        )
        try:
            result = await self.hass.async_add_executor_job(engine.run, inputs)
        except engine.ModelError as err:
            raise HomeAssistantError(str(err)) from err
        result["message"] = None
        return result

    def _sensor_points(self, states: list) -> list[tuple[float, float | None]]:
        """Recorder states of a temperature sensor as sorted points in the Home Assistant unit."""
        unit = self.hass.config.units.temperature_unit
        out = []
        for st in states:
            v = _num(st.state)
            src = st.attributes.get("unit_of_measurement")
            if v is not None and src and src != unit and src in TemperatureConverter.VALID_UNITS:
                v = TemperatureConverter.convert(v, src, unit)
            out.append((st.last_updated.timestamp(), v))
        out.sort(key=lambda p: p[0])
        return out

    async def _fetch_weather(self, history_days: int) -> tuple[list, list, list]:
        fahrenheit = self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT
        params = {
            "latitude": self.hass.config.latitude,
            "longitude": self.hass.config.longitude,
            "hourly": "temperature_2m,shortwave_radiation",
            "temperature_unit": "fahrenheit" if fahrenheit else "celsius",
            "past_days": min(history_days + 1, 92),
            "forecast_days": 3,
            "timezone": "UTC",
        }
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(OPEN_METEO_URL, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HomeAssistantError(f"Could not fetch weather from Open-Meteo: {err}") from err
        h = payload["hourly"]
        ts, temp, sun = [], [], []
        for t, tv, sv in zip(h["time"], h["temperature_2m"], h["shortwave_radiation"]):
            if tv is None or sv is None:
                continue
            ts.append(datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp())
            temp.append(float(tv))
            sun.append(float(sv))
        if len(ts) < 48:
            raise HomeAssistantError("Open-Meteo returned too little weather data")
        return ts, temp, sun

    # ---------------------------------------------------------- applying
    async def async_scheduled_run(self, _now: datetime | None = None) -> None:
        """Daily timer: refresh, and replace the schedule if auto-apply is on."""
        await self.async_refresh()
        if self.scheduler.auto_apply and (self.data or {}).get("ok_to_apply"):
            try:
                await self.async_apply()
            except HomeAssistantError as err:
                _LOGGER.warning("Auto-apply skipped: %s", err)

    async def async_apply(self) -> None:
        """Make the proposed schedule the active one."""
        data = self.data or {}
        best = data.get("best")
        if not best:
            raise HomeAssistantError("There is no proposed schedule to apply.")
        if not data.get("validated"):
            raise HomeAssistantError(
                "The house model did not match your recent AC runtime closely enough, so the proposal is not trusted yet."
            )
        await self.scheduler.async_set_nodes(best["nodes"], source="optimizer")
