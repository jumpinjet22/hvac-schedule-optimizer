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
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
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
    CONF_AUX_BELOW,
    CONF_AWAKE_MIN,
    CONF_AWAY_MAX,
    CONF_AWAY_MIN,
    CONF_DIRECTION,
    CONF_PEOPLE,
    CONF_TRAVEL,
    CONF_BASE_DAY_HEAT,
    CONF_BASE_NIGHT_HEAT,
    CONF_CLIMATE,
    CONF_DEADBAND,
    CONF_HEAT_KIND,
    CONF_SLEEP_MIN,
    CONF_SWITCH_BELOW,
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
    auto_switching,
    default_options,
    guess_fan_modes,
    heat_ratios,
    plan_mode,
    run_mode,
    setpoints_of,
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
    heat = o[CONF_HEAT_KIND] != "none"
    return [
        {
            "time": engine.hhmm(int(o[CONF_BASE_WAKE_HOUR]) * 60),
            "temp": float(o[CONF_BASE_DAY_TEMP]),
            **({"heat": float(o[CONF_BASE_DAY_HEAT])} if heat else {}),
        },
        {
            "time": engine.hhmm(int(o[CONF_BASE_SLEEP_HOUR]) * 60),
            "temp": float(o[CONF_BASE_NIGHT_TEMP]),
            **({"heat": float(o[CONF_BASE_NIGHT_HEAT])} if heat else {}),
        },
    ]


REFRESH = timedelta(minutes=15)
FORECAST_DAYS = 14  # stored plan forecasts, enough weeks of predicted vs actual to learn from
COMFORT_DAYS = 60
COMFORT_MAX_EVENTS = 200
UNDO_S = 600  # a change reversed within this long was a slip, not a preference
PRESENCE_DAYS = 28  # four of each weekday to learn routines from
AWAY_DELAY_S = 600  # the house counts as empty once the last person has been gone this long
TRAVEL_STALE_S = 1800  # a travel time older than this is ignored


def _minutes(state) -> float | None:
    """A travel time sensor's value in minutes (Waze, Google and HERE all report minutes)."""
    v = _num(state.state) if state is not None else None
    if v is None:
        return None
    unit = state.attributes.get("unit_of_measurement")
    return v * 60 if unit == "h" else v / 60 if unit == "s" else v


def _at_home(state) -> bool:
    """Unknown or unavailable counts as home: never assume away when unsure."""
    return state is None or state.state in ("home", STATE_UNAVAILABLE, STATE_UNKNOWN)


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
        self._away_timer: CALLBACK_TYPE | None = None

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
    async def async_record_change(self, old: float, new: float, source: str, side: str = "cool") -> None:
        """A manual setpoint change: keep it with a snapshot of what the house was doing."""
        if not self.learn_enabled:
            return
        now = dt_util.utcnow().timestamp()
        local = dt_util.now()
        lm = local.hour * 60 + local.minute
        if self.comfort_events:
            last = self.comfort_events[-1]
            if now - last["ts"] < UNDO_S and abs(last["from"] - new) < 1e-6 and last.get("side", "cool") == side:
                self.comfort_events.pop()  # undone straight away
                self._comfort.async_delay_save(self._comfort_data, 5)
                self.async_update_listeners()
                return
        self.comfort_events.append(
            {"ts": now, "lm": lm, "from": old, "to": new, "source": source, "side": side, **self._snapshot()}
        )
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
        s.mode = plan_mode(climate_state, o) or "cool"
        s.awake_min = float(o[CONF_AWAKE_MIN])
        s.sleep_min = float(o[CONF_SLEEP_MIN])
        s.heat_baseline = [
            (int(o[CONF_BASE_WAKE_HOUR]) * 60, float(o[CONF_BASE_DAY_HEAT])),
            (int(o[CONF_BASE_SLEEP_HOUR]) * 60, float(o[CONF_BASE_NIGHT_HEAT])),
        ]
        # Only one setpoint is live at a time while this integration switches modes itself.
        s.min_deadband = 0.0 if auto_switching(climate_state, o) else float(o[CONF_DEADBAND])
        s.heat_kind = o[CONF_HEAT_KIND] if o[CONF_HEAT_KIND] != "none" else "furnace"
        for k, v in heat_ratios(o, f).items():
            setattr(s, k, v)
        s.aux_below = float(o[CONF_AUX_BELOW])
        s.switch_below = float(o[CONF_SWITCH_BELOW])
        s.away_max = float(o[CONF_AWAY_MAX])
        s.away_min = float(o[CONF_AWAY_MIN])
        return s

    def deadband(self) -> float:
        """Smallest gap the thermostat accepts between its heat and cool setpoints (none while auto switching)."""
        f = self._unit == UnitOfTemperature.FAHRENHEIT
        o = {**default_options(f), **self.config_entry.options}
        return 0.0 if auto_switching(self.hass.states.get(self.climate_entity), o) else float(o[CONF_DEADBAND])

    def _auto_mode_wanted(self, result: dict[str, Any]) -> tuple[str | None, str]:
        """Heat or cool, from what the running schedule's forecast needs over the next 2 hours."""
        series = result.get("series") or {}
        ni = series.get("now_index", 0)
        heat = ((series.get("heat") or {}).get("active") or [])[ni : ni + 8]
        ac = ((series.get("ac") or {}).get("active") or [])[ni : ni + 8]
        h, c = sum(v or 0 for v in heat), sum(v or 0 for v in ac)
        if h >= 0.5 and c == 0:
            return "heat", "the plan needs heat in the next 2 hours"
        if c >= 0.5 and h == 0:
            return "cool", "the plan needs cooling in the next 2 hours"
        return None, ""

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
            o = {**default_options(self._unit == UnitOfTemperature.FAHRENHEIT), **self.config_entry.options}
            if auto_switching(self.hass.states.get(self.climate_entity), o):
                want, why = self._auto_mode_wanted(result)
                if want:
                    self.hass.async_create_task(self.scheduler.async_auto_switch(want, why))
            self.hass.async_create_task(self._async_apply_away(result))
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
        o_all = {**self.config_entry.options}
        if plan_mode(state, o_all) is None:
            raise HomeAssistantError(
                f"{self.climate_entity} is in '{state.state}' mode. The optimizer plans for cool, heat and heat_cool."
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
        heat, heat_sp, mode_pts = [], [], []
        for st in states.get(self.climate_entity, []):
            ts = st.last_updated.timestamp()
            if st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                indoor.append((ts, None))
                on.append((ts, None))
                setp.append((ts, None))
                fan.append((ts, None))
                heat.append((ts, None))
                heat_sp.append((ts, None))
                mode_pts.append((ts, None))
                continue
            a = st.attributes
            if not sensor:
                indoor.append((ts, _num(a.get("current_temperature"))))
            else:
                thermostat_temp.append((ts, _num(a.get("current_temperature"))))
            action = a.get("hvac_action")
            on.append((ts, None if action is None else (1.0 if action == "cooling" else 0.0)))
            heat.append((ts, None if action is None else (1.0 if action in ("heating", "preheating") else 0.0)))
            cool_t, heat_t = setpoints_of(st)
            setp.append((ts, cool_t))
            heat_sp.append((ts, heat_t))
            mode_pts.append((ts, run_mode(st) or "off"))
            if modes:
                fm = a.get("fan_mode")
                forced = action == "fan" or (circ_mode is not None and fm == circ_mode)
                fan.append((ts, 1.0 if forced else (0.0 if fm is not None or action is not None else None)))
        if sensor:
            indoor = self._sensor_points(states.get(sensor, []))
        for series in (indoor, on, setp, heat, heat_sp, mode_pts):
            series.sort(key=lambda p: p[0])

        wts, wtemp, wsun = await self._fetch_weather(days)
        people = await self._people_inputs(recorder, o, now)

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
            heat=heat,  # always: heating periods must not look like "AC off" to the cooling fit
            heat_setpoint=heat_sp,
            modes=mode_pts,
            people=people,
            guest=self.scheduler.guest,
            presence_days=PRESENCE_DAYS,
        )
        try:
            result = await self.hass.async_add_executor_job(engine.run, inputs)
        except engine.ModelError as err:
            raise HomeAssistantError(str(err)) from err
        result["message"] = None
        self._last_error = None
        return result

    # ---------------------------------------------------------- presence
    def people(self) -> list[str]:
        return list(self.config_entry.options.get(CONF_PEOPLE) or [])

    def _person_home(self, entity_id: str, now: float) -> bool:
        """Home, or left less than AWAY_DELAY_S ago (a quick trip does not count)."""
        st = self.hass.states.get(entity_id)
        return _at_home(st) or now - st.last_changed.timestamp() < AWAY_DELAY_S

    async def _people_inputs(self, recorder, o: dict, now: float) -> list[dict]:
        ids = self.people()
        if not ids:
            return []
        start = now - PRESENCE_DAYS * 86400
        hist = await recorder.async_add_executor_job(
            partial(
                history.get_significant_states,
                self.hass,
                dt_util.utc_from_timestamp(start),
                dt_util.utc_from_timestamp(now),
                entity_ids=ids,
                include_start_time_state=True,
                significant_changes_only=False,
                minimal_response=False,
                no_attributes=True,
            )
        )
        out = []
        for pid in ids:
            points = [
                (st.last_updated.timestamp(), None if st.state in (STATE_UNAVAILABLE, STATE_UNKNOWN) else float(st.state == "home"))
                for st in hist.get(pid, [])
            ]
            points.sort(key=lambda p: p[0])
            eta = towards = None
            if (sensor := (o.get(CONF_TRAVEL) or {}).get(pid)) and (ts_ := self.hass.states.get(sensor)):
                if now - ts_.last_updated.timestamp() < TRAVEL_STALE_S:
                    eta = _minutes(ts_)
            if (sensor := (o.get(CONF_DIRECTION) or {}).get(pid)) and (ds := self.hass.states.get(sensor)):
                towards = True if ds.state == "towards" else False if ds.state in ("away_from", "stationary") else None
            st = self.hass.states.get(pid)
            out.append(
                {
                    "id": pid,
                    "name": st.name if st else pid,
                    "points": points,
                    "home_now": self._person_home(pid, now),
                    "eta_min": eta,
                    "towards": towards,
                }
            )
        return out

    @callback
    def async_listen_presence(self) -> CALLBACK_TYPE:
        """Re-plan when people come and go, and as someone out heads home."""
        o = self.config_entry.options
        people = self.people()
        extra = [e for p in people for e in ((o.get(CONF_TRAVEL) or {}).get(p), (o.get(CONF_DIRECTION) or {}).get(p)) if e]
        if not people:
            return lambda: None
        unsub = async_track_state_change_event(self.hass, people + extra, self._async_presence_changed)

        def stop() -> None:
            unsub()
            if self._away_timer:
                self._away_timer()
                self._away_timer = None

        return stop

    async def _async_presence_changed(self, event: Event[EventStateChangedData]) -> None:
        people = self.people()
        sch = self.scheduler
        if any(_at_home(self.hass.states.get(p)) for p in people):
            if self._away_timer:
                self._away_timer()
                self._away_timer = None
            # Someone is home: back to the schedule straight away, without waiting for the optimizer.
            sch.away_cancelled = False
            if sch.away:
                await sch.async_set_away(None)
            if event.data["entity_id"] in people and not _at_home(event.data["old_state"]):
                await self.async_plan("arrival")
            return
        if event.data["entity_id"] in people:
            # The last person left: plan the setback once they have been gone long enough.
            if self._away_timer:
                self._away_timer()

            async def _later(_now) -> None:
                self._away_timer = None
                await self.async_plan("away")

            self._away_timer = async_call_later(self.hass, AWAY_DELAY_S + 5, _later)
        else:
            await self.async_request_refresh()  # a travel time or direction changed while everyone is out

    async def _async_apply_away(self, result: dict[str, Any]) -> None:
        """Hand the planned setback to the scheduler, or end it."""
        if result.get("status") == "error":
            return  # keep what runs; a setback ends on its own at its end time
        sch = self.scheduler
        pr = result.get("presence") or {}
        away = result.get("away")
        if not (pr.get("house_away") and away and sch.enabled and result.get("validated")) or sch.away_cancelled or sch.guest:
            if sch.away:
                await sch.async_set_away(None)
            return
        start = sch.away["start"] if sch.away else away["start_ts"]
        await sch.async_set_away(
            {"start": start, "end": away["end_ts"], "return": away["return_ts"], "temp": away["temp"], "heat": away["heat"]}
        )

    async def async_set_guest(self, value: bool) -> None:
        await self.scheduler.async_set_guest(value)
        await self.async_refresh()

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
