"""Config and options flow."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AWAKE_END,
    CONF_AWAKE_MAX,
    CONF_AWAKE_START,
    CONF_BASE_DAY_TEMP,
    CONF_BASE_NIGHT_TEMP,
    CONF_BASE_SLEEP_HOUR,
    CONF_BASE_WAKE_HOUR,
    CONF_AC_KW,
    CONF_AUTO_MODE,
    CONF_AUX_BELOW,
    CONF_AUX_KW,
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
    CONF_ELEC_HEAT_KW,
    CONF_ELEC_PRICE,
    CONF_FURNACE_SIZE,
    CONF_GAS_PRICE,
    CONF_HEAT_KIND,
    CONF_SLEEP_MIN,
    CONF_SWITCH_BELOW,
    HEAT_KINDS,
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
    CONF_RUN_TIME,
    CONF_SLEEP_END,
    CONF_SLEEP_MAX,
    CONF_SLEEP_START,
    DOMAIN,
    default_options,
    guess_fan_modes,
    thermostat_fan_modes,
    thermostat_modes,
)


def _box(lo: float, hi: float, step: float = 1) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=lo, max=hi, step=step, mode=selector.NumberSelectorMode.BOX)
    )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_CLIMATE])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=user_input[CONF_CLIMATE], data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_CLIMATE): selector.EntitySelector(selector.EntitySelectorConfig(domain="climate")),
                vol.Optional(CONF_INDOOR_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return OptionsFlowHandler()


def _temp_sensors() -> selector.EntitySelector:
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain="sensor", device_class="temperature", multiple=True)
    )


class OptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._people_left: list[str] = []

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._data = {**user_input, CONF_ROOM_WHEN: {}}
            if user_input.get(CONF_ROOM_SENSORS):
                return await self.async_step_rooms()
            return await self.async_step_presence()

        fahrenheit = self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT
        cur = {**default_options(fahrenheit), **self.config_entry.options}
        t_lo, t_hi, t_step = (55, 90, 1) if fahrenheit else (13, 32, 0.5)

        schema = vol.Schema(
            {
                vol.Required(CONF_AWAKE_START, default=cur[CONF_AWAKE_START]): _box(0, 23),
                vol.Required(CONF_AWAKE_END, default=cur[CONF_AWAKE_END]): _box(1, 24),
                vol.Required(CONF_AWAKE_MAX, default=cur[CONF_AWAKE_MAX]): _box(t_lo, t_hi, t_step),
                vol.Required(CONF_SLEEP_START, default=cur[CONF_SLEEP_START]): _box(0, 23),
                vol.Required(CONF_SLEEP_END, default=cur[CONF_SLEEP_END]): _box(0, 23),
                vol.Required(CONF_SLEEP_MAX, default=cur[CONF_SLEEP_MAX]): _box(t_lo, t_hi, t_step),
                vol.Required(CONF_HYSTERESIS, default=cur[CONF_HYSTERESIS]): _box(0.1, 3, 0.05),
                vol.Required(CONF_BASE_WAKE_HOUR, default=cur[CONF_BASE_WAKE_HOUR]): _box(0, 23),
                vol.Required(CONF_BASE_DAY_TEMP, default=cur[CONF_BASE_DAY_TEMP]): _box(t_lo, t_hi, t_step),
                vol.Required(CONF_BASE_SLEEP_HOUR, default=cur[CONF_BASE_SLEEP_HOUR]): _box(0, 23),
                vol.Required(CONF_BASE_NIGHT_TEMP, default=cur[CONF_BASE_NIGHT_TEMP]): _box(t_lo, t_hi, t_step),
                vol.Required(CONF_RUN_TIME, default=cur[CONF_RUN_TIME]): selector.TimeSelector(),
                vol.Required(CONF_HISTORY_DAYS, default=cur[CONF_HISTORY_DAYS]): _box(3, 30),
                vol.Optional(CONF_ROOM_SENSORS, default=list(cur[CONF_ROOM_SENSORS])): _temp_sensors(),
                vol.Optional(CONF_OUTDOOR_SENSORS, default=list(cur[CONF_OUTDOOR_SENSORS])): _temp_sensors(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_rooms(self, user_input: dict[str, Any] | None = None):
        """One dropdown per room: when its temperature counts against the comfort limits."""
        rooms = self._data[CONF_ROOM_SENSORS]
        if user_input is not None:
            self._data[CONF_ROOM_WHEN] = dict(user_input)
            return await self.async_step_presence()

        current = self.config_entry.options.get(CONF_ROOM_WHEN, {})
        when = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=["always", "awake", "asleep", "never"],
                translation_key="room_when",
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
        schema = vol.Schema({vol.Required(r, default=current.get(r, "always")): when for r in rooms})
        names = ", ".join(
            f"{r} = {(st.name if (st := self.hass.states.get(r)) else r)}" for r in rooms
        )
        return self.async_show_form(step_id="rooms", data_schema=schema, description_placeholders={"names": names})

    async def async_step_presence(self, user_input: dict[str, Any] | None = None):
        """Who lives here, and how loose the limits get while everyone is out."""
        if user_input is not None:
            self._data.update(user_input)
            self._data.setdefault(CONF_PEOPLE, [])
            self._data[CONF_TRAVEL], self._data[CONF_DIRECTION] = {}, {}
            self._people_left = list(self._data[CONF_PEOPLE])
            return await self.async_step_person()
        cur, fahrenheit = self._cur()
        t_lo, t_hi, t_step = (45, 95, 1) if fahrenheit else (7, 35, 0.5)
        fields: dict[Any, Any] = {
            vol.Optional(CONF_PEOPLE, default=list(cur[CONF_PEOPLE])): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="person", multiple=True)
            ),
            vol.Required(CONF_AWAY_MAX, default=cur[CONF_AWAY_MAX]): _box(t_lo, t_hi, t_step),
        }
        modes = thermostat_modes(self.hass.states.get(self.config_entry.data[CONF_CLIMATE]))
        if "heat" in modes or "heat_cool" in modes:
            fields[vol.Required(CONF_AWAY_MIN, default=cur[CONF_AWAY_MIN])] = _box(t_lo, t_hi, t_step)
        return self.async_show_form(step_id="presence", data_schema=vol.Schema(fields))

    async def async_step_person(self, user_input: dict[str, Any] | None = None):
        """One page per person: their travel time and direction sensors, both optional."""
        if user_input is not None:
            pid = self._people_left.pop(0)
            if user_input.get("travel_sensor"):
                self._data[CONF_TRAVEL][pid] = user_input["travel_sensor"]
            if user_input.get("direction_sensor"):
                self._data[CONF_DIRECTION][pid] = user_input["direction_sensor"]
        if not self._people_left:
            return await self.async_step_fan()
        pid = self._people_left[0]
        old = self.config_entry.options
        fields: dict[Any, Any] = {}
        for key, saved in (
            ("travel_sensor", (old.get(CONF_TRAVEL) or {}).get(pid)),
            ("direction_sensor", (old.get(CONF_DIRECTION) or {}).get(pid)),
        ):
            k = vol.Optional(key, default=saved) if saved else vol.Optional(key)
            fields[k] = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
        st = self.hass.states.get(pid)
        return self.async_show_form(
            step_id="person", data_schema=vol.Schema(fields), description_placeholders={"name": st.name if st else pid}
        )

    async def async_step_fan(self, user_input: dict[str, Any] | None = None):
        """Only shown when the thermostat reports fan modes; the dropdowns list its own mode names."""
        modes = thermostat_fan_modes(self.hass.states.get(self.config_entry.data[CONF_CLIMATE]))
        if not modes:
            self._data[CONF_FAN_CONTROL] = False
            return await self.async_step_heating()
        if user_input is not None:
            if user_input[CONF_FAN_CONTROL] and user_input[CONF_FAN_MODE_NORMAL] == user_input[CONF_FAN_MODE_CIRCULATE]:
                return self._fan_form(modes, user_input, {"base": "same_fan_mode"})
            self._data.update(user_input)
            return await self.async_step_heating()
        return self._fan_form(modes, None, {})

    def _cur(self) -> tuple[dict[str, Any], bool]:
        fahrenheit = self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT
        return {**default_options(fahrenheit), **self.config_entry.options, **self._data}, fahrenheit

    async def async_step_heating(self, user_input: dict[str, Any] | None = None):
        """Heat source and heating comfort. Skipped for thermostats that cannot heat."""
        modes = thermostat_modes(self.hass.states.get(self.config_entry.data[CONF_CLIMATE]))
        if "heat" not in modes and "heat_cool" not in modes:
            self._data[CONF_HEAT_KIND] = "none"
            return await self.async_step_costs()
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_costs()
        cur, fahrenheit = self._cur()
        t_lo, t_hi, t_step = (45, 85, 1) if fahrenheit else (7, 29, 0.5)
        kind = selector.SelectSelector(
            selector.SelectSelectorConfig(options=HEAT_KINDS, translation_key="heat_kind", mode=selector.SelectSelectorMode.DROPDOWN)
        )
        fields: dict[Any, Any] = {
            vol.Required(CONF_HEAT_KIND, default=cur[CONF_HEAT_KIND]): kind,
            vol.Required(CONF_AWAKE_MIN, default=cur[CONF_AWAKE_MIN]): _box(t_lo, t_hi, t_step),
            vol.Required(CONF_SLEEP_MIN, default=cur[CONF_SLEEP_MIN]): _box(t_lo, t_hi, t_step),
            vol.Required(CONF_BASE_DAY_HEAT, default=cur[CONF_BASE_DAY_HEAT]): _box(t_lo, t_hi, t_step),
            vol.Required(CONF_BASE_NIGHT_HEAT, default=cur[CONF_BASE_NIGHT_HEAT]): _box(t_lo, t_hi, t_step),
            vol.Required(CONF_DEADBAND, default=cur[CONF_DEADBAND]): _box(0.5, 10, 0.5),
        }
        # Thermostats without a two-setpoint mode can have this integration switch heat and cool.
        if "heat_cool" not in modes and "heat" in modes and "cool" in modes:
            fields[vol.Required(CONF_AUTO_MODE, default=bool(cur[CONF_AUTO_MODE]))] = selector.BooleanSelector()
        return self.async_show_form(
            step_id="heating", data_schema=vol.Schema(fields), description_placeholders={"modes": ", ".join(modes)}
        )

    async def async_step_costs(self, user_input: dict[str, Any] | None = None):
        """Prices and equipment sizes, only the ones the chosen heat source needs."""
        if user_input is not None:
            return self.async_create_entry(title="", data={**self._data, **user_input})
        cur, fahrenheit = self._cur()
        kind = cur[CONF_HEAT_KIND]
        fields: dict[Any, Any] = {
            vol.Required(CONF_AC_KW, default=cur[CONF_AC_KW]): _box(0.5, 15, 0.1),
            vol.Required(CONF_ELEC_PRICE, default=cur[CONF_ELEC_PRICE]): _box(0.01, 2, 0.005),
        }
        if kind in ("furnace", "dual_fuel"):
            fields[vol.Required(CONF_GAS_PRICE, default=cur[CONF_GAS_PRICE])] = _box(0.001, 10, 0.001)
            fields[vol.Required(CONF_FURNACE_SIZE, default=cur[CONF_FURNACE_SIZE])] = (
                _box(10000, 250000, 1000) if fahrenheit else _box(3, 75, 0.1)
            )
        if kind == "electric":
            fields[vol.Required(CONF_ELEC_HEAT_KW, default=cur[CONF_ELEC_HEAT_KW])] = _box(1, 40, 0.5)
        o_lo, o_hi = (-20, 60) if fahrenheit else (-30, 15)
        if kind == "heat_pump":
            fields[vol.Required(CONF_AUX_KW, default=cur[CONF_AUX_KW])] = _box(0, 40, 0.5)
            fields[vol.Required(CONF_AUX_BELOW, default=cur[CONF_AUX_BELOW])] = _box(o_lo, o_hi, 0.5)
        if kind == "dual_fuel":
            fields[vol.Required(CONF_SWITCH_BELOW, default=cur[CONF_SWITCH_BELOW])] = _box(o_lo, o_hi, 0.5)
        units = ("$/therm", "BTU/h") if fahrenheit else ("per kWh", "kW")
        return self.async_show_form(
            step_id="costs",
            data_schema=vol.Schema(fields),
            description_placeholders={"gas_unit": units[0], "furnace_unit": units[1]},
        )

    def _fan_form(self, modes: list[str], given: dict[str, Any] | None, errors: dict[str, str]):
        fahrenheit = self.hass.config.units.temperature_unit == UnitOfTemperature.FAHRENHEIT
        cur = {**default_options(fahrenheit), **self.config_entry.options, **(given or {})}
        normal, circ = guess_fan_modes(modes)
        normal = cur.get(CONF_FAN_MODE_NORMAL) if cur.get(CONF_FAN_MODE_NORMAL) in modes else normal
        circ = cur.get(CONF_FAN_MODE_CIRCULATE) if cur.get(CONF_FAN_MODE_CIRCULATE) in modes else circ
        pick = selector.SelectSelector(
            selector.SelectSelectorConfig(options=modes, mode=selector.SelectSelectorMode.DROPDOWN)
        )
        fields: dict[Any, Any] = {
            vol.Required(CONF_FAN_CONTROL, default=bool(cur[CONF_FAN_CONTROL])): selector.BooleanSelector(),
            vol.Required(CONF_FAN_MODE_NORMAL, default=normal): pick,
            vol.Required(CONF_FAN_MODE_CIRCULATE, default=circ or normal): pick,
            vol.Required(CONF_FAN_WATTS, default=cur[CONF_FAN_WATTS]): _box(20, 1500, 10),
            vol.Required(CONF_FAN_HUMIDITY_MAX, default=cur[CONF_FAN_HUMIDITY_MAX]): _box(30, 90, 1),
        }
        sensor_key = (
            vol.Optional(CONF_HUMIDITY_SENSOR, default=cur[CONF_HUMIDITY_SENSOR])
            if cur.get(CONF_HUMIDITY_SENSOR)
            else vol.Optional(CONF_HUMIDITY_SENSOR)
        )
        fields[sensor_key] = selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class="humidity")
        )
        return self.async_show_form(
            step_id="fan",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"modes": ", ".join(modes)},
        )
