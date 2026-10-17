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
    CONF_CLIMATE,
    CONF_HISTORY_DAYS,
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
            self._data = user_input
            if user_input.get(CONF_ROOM_SENSORS):
                return await self.async_step_rooms()
            return self.async_create_entry(title="", data={**user_input, CONF_ROOM_WHEN: {}})

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
            return self.async_create_entry(title="", data={**self._data, CONF_ROOM_WHEN: dict(user_input)})

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
