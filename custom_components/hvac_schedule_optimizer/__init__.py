"""HVAC Schedule Optimizer: a self-contained, weather-aware thermostat scheduler."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path

import voluptuous as vol
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType

from . import websocket_api
from .const import CONF_CLIMATE, CONF_RUN_TIME, DOMAIN, default_options
from .coordinator import OptimizerCoordinator, baseline_nodes
from .scheduler import Scheduler

CARD_FILE = "hvac-schedule-card.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILE}"
CARD_VERSION = "0.3.0"

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.SWITCH, Platform.BUTTON]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_SET_SCHEDULE = "set_schedule"

_NODE = vol.Schema(
    {
        vol.Required("time"): vol.All(cv.time, lambda t: t.strftime("%H:%M")),
        vol.Required("temp"): vol.Coerce(float),
    }
)
_SET_SCHEDULE = vol.Schema(
    {
        vol.Required("nodes"): vol.All(cv.ensure_list, [_NODE], vol.Length(min=1)),
        vol.Optional("climate_entity"): cv.entity_id,
    }
)


@dataclass
class RuntimeData:
    coordinator: OptimizerCoordinator
    scheduler: Scheduler


type OptimizerConfigEntry = ConfigEntry[RuntimeData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def _set_schedule(call: ServiceCall) -> None:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        wanted = call.data.get("climate_entity")
        targets = [e for e in entries if not wanted or e.data[CONF_CLIMATE] == wanted]
        if not targets:
            raise HomeAssistantError("No matching HVAC Schedule Optimizer entry is loaded.")
        for entry in targets:
            await entry.runtime_data.scheduler.async_set_nodes(call.data["nodes"], source="manual")

    hass.services.async_register(DOMAIN, SERVICE_SET_SCHEDULE, _set_schedule, schema=_SET_SCHEDULE)
    websocket_api.async_register(hass)

    # Serve the dashboard card from inside the integration and load it on every frontend,
    # so installing through HACS is the only step (no Lovelace resource to add by hand).
    card = Path(__file__).parent / "frontend" / CARD_FILE
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(card), False)])
    add_extra_js_url(hass, f"{CARD_URL}?v={CARD_VERSION}")
    # Also list it as a dashboard resource. Dashboards wait for resources before building cards,
    # while extra JS can land after them (the companion app often hits that race on resume).
    await _async_register_resource(hass, f"{CARD_URL}?v={CARD_VERSION}")
    return True


async def _async_register_resource(hass: HomeAssistant, url: str) -> None:
    """Add or update the card in the Lovelace resources (storage mode only)."""
    data = hass.data.get("lovelace")
    resources = data.get("resources") if isinstance(data, dict) else getattr(data, "resources", None)
    if resources is None or not hasattr(resources, "async_create_item"):
        return  # YAML mode: the user manages resources, extra JS still loads the card
    try:
        if not resources.loaded:
            await resources.async_load()
            resources.loaded = True
        for item in resources.async_items():
            if item["url"].split("?")[0] == CARD_URL:
                if item["url"] != url:
                    await resources.async_update_item(item["id"], {"res_type": "module", "url": url})
                return
        await resources.async_create_item({"res_type": "module", "url": url})
    except Exception:  # noqa: BLE001 - never block setup over the card
        _LOGGER.warning("Could not add the HVAC Schedule card to dashboard resources", exc_info=True)


async def async_setup_entry(hass: HomeAssistant, entry: OptimizerConfigEntry) -> bool:
    scheduler = Scheduler(hass, entry.entry_id, entry.data[CONF_CLIMATE], baseline_nodes(hass, entry.options))
    await scheduler.async_load()
    coordinator = OptimizerCoordinator(hass, entry, scheduler)
    entry.runtime_data = RuntimeData(coordinator, scheduler)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    f = hass.config.units.temperature_unit == "°F"
    run_time = {**default_options(f), **entry.options}[CONF_RUN_TIME]
    hour, minute = (int(x) for x in str(run_time).split(":")[:2])
    entry.async_on_unload(
        async_track_time_change(hass, coordinator.async_scheduled_run, hour=hour, minute=minute, second=0)
    )
    entry.async_on_unload(scheduler.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_reload))

    async def _started(_: HomeAssistant) -> None:
        await scheduler.async_start()
        await coordinator.async_refresh()

    if cancel := async_at_started(hass, _started):
        entry.async_on_unload(cancel)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OptimizerConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: OptimizerConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
