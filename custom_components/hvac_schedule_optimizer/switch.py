"""Switches: run the schedule, and let the optimizer replace it automatically."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import OptimizerCoordinator


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: OptimizerCoordinator = entry.runtime_data.coordinator
    async_add_entities([SchedulerSwitch(coordinator), AutoApplySwitch(coordinator)])


class _Base(SwitchEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: OptimizerCoordinator, key: str, name: str, icon: str) -> None:
        self._coordinator = coordinator
        self._scheduler = coordinator.scheduler
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="HVAC Schedule Optimizer",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._scheduler.async_add_listener(self.async_write_ha_state))


class SchedulerSwitch(_Base):
    """When on, this integration drives the thermostat from the active schedule."""

    def __init__(self, coordinator: OptimizerCoordinator) -> None:
        super().__init__(coordinator, "scheduler", "Run schedule", "mdi:calendar-sync")

    @property
    def is_on(self) -> bool:
        return self._scheduler.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._scheduler.async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._scheduler.async_set_enabled(False)


class AutoApplySwitch(_Base):
    """When on, the daily optimizer run replaces the active schedule if the model is trusted."""

    def __init__(self, coordinator: OptimizerCoordinator) -> None:
        super().__init__(coordinator, "auto_apply", "Auto apply optimizer", "mdi:auto-fix")

    @property
    def is_on(self) -> bool:
        return self._scheduler.auto_apply

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._scheduler.async_set_auto_apply(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._scheduler.async_set_auto_apply(False)
