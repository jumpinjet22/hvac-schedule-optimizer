"""Coordinator: pulls history and weather, runs the optimizer, hands results to the scheduler."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
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
from homeassistant.helpers.storage import Store
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
    CONF_AC_KW,
    CONF_CLIMATE,
    CONF_FAN_CONTROL,
    CONF_FAN_HUMIDITY_MAX,
    CONF_FAN_MODE_CIRCULATE,
    CONF_FAN_MODE_NORMAL,
    CONF_FAN_WATTS,
    CONF_HISTORY_DAYS,
    CONF_HUMIDITY_SENSOR,
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
    STORAGE_VERSION,
    default_options,
    guess_fan_modes,
    thermostat_fan_modes,
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


REFRESH = timedelta(minutes=15)
FORECAST_DAYS = 14  # stored plan forecasts, enough weeks of predicted vs actual to learn from
COMFORT_DAYS = 60
COMFORT_MAX_EVENTS = 200
UNDO_S = 600  # a change reversed within this long was a slip, not a preference


class OptimizerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Runs the optimizer on demand (button), every REFRESH so the chart keeps up with
    the clock, and on a daily timer (the only run that may auto-apply)."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, scheduler: Scheduler) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry, update_interval=REFRESH)
        self.scheduler = scheduler
        self._last_error: str | None = None
        # Why the next refresh runs. Only planning runs (daily, startup, Optimize, apply) store
        # their forecast; the 15 minute refreshes start from a fresh reading and would hide misses.
        self._plan_reason: str | None = None
        self._forecasts: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.forecasts")
        self._comfort: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.comfort")
        self.forecast_bins: dict[int, dict] = {}
        self.comfort_events: list[dict] = []
        self.comfort_forgotten: dict[str, float] = {}
        self.learn_enabled = True

    @property
    def _unit(self) -> str:
        return self.hass.config.units.temperature_unit

    # ---------------------------------------------------------- storage
    async def async_load_stores(self) -> None:
        f = await self._forecasts.async_load() or {}
        if f.get("unit") == self._unit:
            self.forecast_bins = {int(k): v for k, v in (f.get("bins") or {}).items()}
        c = await self._comfort.async_load() or {}
        if c.get("unit") in (None, self._unit):
            self.comfort_events = list(c.get("events") or [])
            self.comfort_forgotten = dict(c.get("forgotten") or {})
        self.learn_enabled = bool(c.get("learn", True))

    def _forecast_data(self) -> dict:
        return {"unit": self._unit, "bins": {str(k): v for k, v in self.forecast_bins.items()}}

    def _comfort_data(self) -> dict:
        return {
            "unit": self._unit,
            "events": self.comfort_events,
            "forgotten": self.comfort_forgotten,
            "learn": self.learn_enabled,
        }

    def _store_plan(self, plan_bins: list[dict], reason: str) -> None:
        """Keep this planning run's forecast for every bin from now on; past bins are never rewritten."""
        now = dt_util.utcnow().timestamp()
        for row in plan_bins:
            if row["ts"] + engine.STEP_S > now:
                self.forecast_bins[int(row["ts"])] = {**{k: v for k, v in row.items() if k != "ts"}, "why": reason, "made": int(now)}
        cutoff = now - FORECAST_DAYS * 86400
        self.forecast_bins = {k: v for k, v in self.forecast_bins.items() if k >= cutoff}
        self._forecasts.async_delay_save(self._forecast_data, 30)

    async def async_plan(self, reason: str = "manual") -> None:
        """A refresh whose forecast becomes the plan the past side of the chart is judged against."""
        self._plan_reason = reason
        await self.async_refresh()

    # ---------------------------------------------------------- comfort learning
    async def async_record_change(self, old: float, new: float, source: str) -> None:
        """A manual setpoint change: keep it with a snapshot of what the house was doing."""
        if not self.learn_enabled:
            return
        now = dt_util.utcnow().timestamp()
        local = dt_util.now()
        lm = local.hour * 60 + local.minute
        if self.comfort_events:
            last = self.comfort_events[-1]
            if now - last["ts"] < UNDO_S and abs(last["from"] - new) < 1e-6:
                self.comfort_events.pop()  # undone straight away
                self._comfort.async_delay_save(self._comfort_data, 5)
                self.async_update_listeners()
                return
        self.comfort_events.append({"ts": now, "lm": lm, "from": old, "to": new, "source": source, **self._snapshot()})
        cutoff = now - COMFORT_DAYS * 86400
        self.comfort_events = [e for e in self.comfort_events if e["ts"] >= cutoff][-COMFORT_MAX_EVENTS:]
        self._comfort.async_delay_save(self._comfort_data, 5)
        self.async_update_listeners()

    def _snapshot(self) -> dict:
        """Indoor, every room and its trend, outdoor, humidity and recent AC at this moment."""
        f = self._unit == UnitOfTemperature.FAHRENHEIT
        o = {**default_options(f), **self.config_entry.options}
        st = self.hass.states.get(self.climate_entity)
        sensor = self.config_entry.data.get(CONF_INDOOR_SENSOR)
        indoor = _num(self.hass.states.get(sensor).state) if sensor and self.hass.states.get(sensor) else None
        if indoor is None and st:
            indoor = _num(st.attributes.get("current_temperature"))
        series = (self.data or {}).get("series") or {}
        ni = series.get("now_index", 0)
        rooms = {}
        for r in series.get("rooms") or []:
            live = self.hass.states.get(r["id"])
            t = _num(live.state) if live else None
            hist = [v for v in (r.get("actual") or [])[max(0, ni - 4) : ni] if v is not None]
            if t is None and hist:
                t = hist[-1]
            if t is not None:
                rooms[r["id"]] = {"t": t, "trend": round(t - hist[0], 2) if hist else 0.0}
        outs = [_num(x.state) for e in o[CONF_OUTDOOR_SENSORS] if (x := self.hass.states.get(e))]
        outs = sorted(v for v in outs if v is not None)
        outdoor = outs[len(outs) // 2] if outs else ((series.get("outdoor") or [None] * (ni + 1))[ni] if series else None)
        ac = [v for v in (series.get("ac_actual") or [])[max(0, ni - 4) : ni] if v is not None]
        return {
            "indoor": indoor,
            "rooms": rooms,
            "outdoor": outdoor,
            "humidity": self._humidity(),
            "ac_duty": round(sum(ac) / len(ac), 2) if ac else None,
            "predicted": ((series.get("indoor") or {}).get("active") or [None] * (ni + 1))[ni] if series else None,
        }

    def _humidity(self) -> float | None:
        f = self._unit == UnitOfTemperature.FAHRENHEIT
        o = {**default_options(f), **self.config_entry.options}
        sensor = o.get(CONF_HUMIDITY_SENSOR)
        if sensor and (st := self.hass.states.get(sensor)) and (v := _num(st.state)) is not None:
            return v
        st = self.hass.states.get(self.climate_entity)
        return _num(st.attributes.get("current_humidity")) if st else None

    async def async_forget_rule(self, rule_id: str) -> None:
        self.comfort_forgotten[rule_id] = dt_util.utcnow().timestamp()
        await self._comfort.async_save(self._comfort_data())
        await self.async_refresh()

    async def async_reset_comfort(self) -> None:
        self.comfort_events = []
        self.comfort_forgotten = {}
        await self._comfort.async_save(self._comfort_data())
        await self.async_refresh()

    async def async_set_learn(self, value: bool) -> None:
        self.learn_enabled = value
        await self._comfort.async_save(self._comfort_data())
        await self.async_refresh()

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
        s.fan_enabled = self.fan_config(climate_state) is not None
        s.fan_kw = float(o[CONF_FAN_WATTS]) / 1000.0
        s.ac_kw = float(o[CONF_AC_KW])
        return s

    def fan_config(self, climate_state=None) -> tuple[str, str] | None:
        """(normal, circulate) when fan control is on and both modes still exist on the thermostat."""
        o = self.config_entry.options
        state = climate_state or self.hass.states.get(self.climate_entity)
        modes = thermostat_fan_modes(state)
        normal, circ = o.get(CONF_FAN_MODE_NORMAL), o.get(CONF_FAN_MODE_CIRCULATE)
        if not o.get(CONF_FAN_CONTROL) or normal not in modes or circ not in modes or normal == circ:
            return None
        return normal, circ

    def fan_blocked(self) -> str | None:
        """Why the fan should not circulate right now, or None."""
        f = self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT
        o = {**default_options(f), **self.config_entry.options}
        limit = float(o[CONF_FAN_HUMIDITY_MAX])
        humidity = self._humidity()
        if humidity is not None and humidity > limit:
            return f"Indoor humidity {humidity:.0f}% is over the {limit:.0f}% fan limit"
        return None

    def _history_days(self) -> int:
        f = self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT
        o = {**default_options(f), **self.config_entry.options}
        return max(3, min(30, int(o[CONF_HISTORY_DAYS])))

    # ---------------------------------------------------------- update
    async def _async_update_data(self) -> dict[str, Any]:
        reason, self._plan_reason = self._plan_reason, None
        try:
            result = await self._compute(quick=reason is None)
            plan_bins = result.pop("plan_bins", None)
            if reason and plan_bins:
                self._store_plan(plan_bins, reason)
        except Exception as err:  # noqa: BLE001 - surface any failure as a status instead of failing setup
            # Log once per distinct failure, not on every refresh.
            if str(err) != self._last_error:
                _LOGGER.warning("Optimizer run failed: %s", err)
            self._last_error = str(err)
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

    async def _compute(self, quick: bool = False) -> dict[str, Any]:
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

        modes = thermostat_fan_modes(state)
        circ_mode = (self.fan_config(state) or (None, guess_fan_modes(modes)[1] if modes else None))[1]
        indoor, on, setp, fan, thermostat_temp = [], [], [], [], []
        for st in states.get(self.climate_entity, []):
            ts = st.last_updated.timestamp()
            if st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                indoor.append((ts, None))
                on.append((ts, None))
                setp.append((ts, None))
                fan.append((ts, None))
                continue
            a = st.attributes
            if not sensor:
                indoor.append((ts, _num(a.get("current_temperature"))))
            else:
                thermostat_temp.append((ts, _num(a.get("current_temperature"))))
            action = a.get("hvac_action")
            on.append((ts, None if action in (None, "heating") else (1.0 if action == "cooling" else 0.0)))
            setp.append((ts, _num(a.get("temperature"))))
            if modes:
                fm = a.get("fan_mode")
                forced = action == "fan" or (circ_mode is not None and fm == circ_mode)
                fan.append((ts, 1.0 if forced else (0.0 if fm is not None or action is not None else None)))
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
            fan=sorted(fan, key=lambda p: p[0]),
            fan_blocked=self.fan_blocked() if settings.fan_enabled else None,
            thermostat_temp=sorted(thermostat_temp, key=lambda p: p[0]),
            past_plan=self.forecast_bins,
            previous_nodes=((self.data or {}).get("best") or {}).get("nodes"),
            comfort_events=self.comfort_events if self.learn_enabled else [],
            comfort_forgotten=self.comfort_forgotten,
            humidity_now=self._humidity(),
            quick=quick,
        )
        try:
            result = await self.hass.async_add_executor_job(engine.run, inputs)
        except engine.ModelError as err:
            raise HomeAssistantError(str(err)) from err
        result["message"] = None
        self._last_error = None
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
        await self.async_plan("daily")
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
        await self.async_plan("apply")

    async def async_revert(self) -> None:
        await self.scheduler.async_revert()
        await self.async_plan("revert")
