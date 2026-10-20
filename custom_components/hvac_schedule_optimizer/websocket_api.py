"""Websocket commands used by the bundled dashboard card."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .const import CONF_CLIMATE, DOMAIN


def _find_entry(hass: HomeAssistant, climate_entity: str | None):
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if climate_entity:
        entries = [e for e in entries if e.data[CONF_CLIMATE] == climate_entity]
    if not entries:
        raise HomeAssistantError("No matching HVAC Schedule Optimizer entry is loaded.")
    return entries[0]


def _payload(entry) -> dict[str, Any]:
    rt = entry.runtime_data
    data = dict(rt.coordinator.data or {})
    return {
        "entry_id": entry.entry_id,
        "result": data,
        "schedule": rt.scheduler.snapshot(),
        "learn": rt.coordinator.learn_enabled,
    }


def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_state)
    websocket_api.async_register_command(hass, ws_optimize)
    websocket_api.async_register_command(hass, ws_apply)
    websocket_api.async_register_command(hass, ws_revert)
    websocket_api.async_register_command(hass, ws_toggle)
    websocket_api.async_register_command(hass, ws_comfort)


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/state", vol.Optional("climate_entity"): str}
)
@callback
def ws_state(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    try:
        entry = _find_entry(hass, msg.get("climate_entity"))
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    connection.send_result(msg["id"], _payload(entry))


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/optimize", vol.Optional("climate_entity"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_optimize(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    try:
        entry = _find_entry(hass, msg.get("climate_entity"))
        await entry.runtime_data.coordinator.async_plan("manual")
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], _payload(entry))


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/apply", vol.Optional("climate_entity"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_apply(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    try:
        entry = _find_entry(hass, msg.get("climate_entity"))
        # Re-runs as a planning run, so the card's "active" trace matches what is now running.
        await entry.runtime_data.coordinator.async_apply()
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], _payload(entry))


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/revert", vol.Optional("climate_entity"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_revert(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    try:
        entry = _find_entry(hass, msg.get("climate_entity"))
        await entry.runtime_data.coordinator.async_revert()
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], _payload(entry))


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/toggle",
        vol.Required("what"): vol.In(["run", "auto_apply", "learn", "guest"]),
        vol.Required("value"): bool,
        vol.Optional("climate_entity"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_toggle(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    try:
        entry = _find_entry(hass, msg.get("climate_entity"))
        sched = entry.runtime_data.scheduler
        if msg["what"] == "run":
            await sched.async_set_enabled(msg["value"])
        elif msg["what"] == "learn":
            await entry.runtime_data.coordinator.async_set_learn(msg["value"])
        elif msg["what"] == "guest":
            await entry.runtime_data.coordinator.async_set_guest(msg["value"])
        else:
            await sched.async_set_auto_apply(msg["value"])
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], _payload(entry))


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/comfort",
        vol.Required("action"): vol.In(["forget", "reset"]),
        vol.Optional("rule_id"): str,
        vol.Optional("climate_entity"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_comfort(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    try:
        entry = _find_entry(hass, msg.get("climate_entity"))
        coordinator = entry.runtime_data.coordinator
        if msg["action"] == "reset":
            await coordinator.async_reset_comfort()
        elif msg.get("rule_id"):
            await coordinator.async_forget_rule(msg["rule_id"])
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], _payload(entry))
