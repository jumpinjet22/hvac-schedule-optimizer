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
    CONF_RUN_TIME,
    CONF_SLEEP_END,
    CONF_SLEEP_MAX,
    CONF_SLEEP_START,
    DOMAIN,
    default_options,
    guess_fan_modes,
    thermostat_fan_modes,
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

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._data = {**user_input, CONF_ROOM_WHEN: {}}
            if user_input.get(CONF_ROOM_SENSORS):
                return await self.async_step_rooms()
            return await self.async_step_fan()

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
            return await self.async_step_fan()

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

    async def async_step_fan(self, user_input: dict[str, Any] | None = None):
        """Only shown when the thermostat reports fan modes; the dropdowns list its own mode names."""
        modes = thermostat_fan_modes(self.hass.states.get(self.config_entry.data[CONF_CLIMATE]))
        if not modes:
            return self.async_create_entry(title="", data={**self._data, CONF_FAN_CONTROL: False})
        if user_input is not None:
            if user_input[CONF_FAN_CONTROL] and user_input[CONF_FAN_MODE_NORMAL] == user_input[CONF_FAN_MODE_CIRCULATE]:
                return self._fan_form(modes, user_input, {"base": "same_fan_mode"})
            return self.async_create_entry(title="", data={**self._data, **user_input})
        return self._fan_form(modes, None, {})

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
            vol.Required(CONF_AC_KW, default=cur[CONF_AC_KW]): _box(0.5, 15, 0.1),
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
