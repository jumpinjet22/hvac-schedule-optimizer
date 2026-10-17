"""Buttons: optimize now, apply the proposal, revert to the previous schedule."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import OptimizerCoordinator


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    c: OptimizerCoordinator = entry.runtime_data.coordinator
    async_add_entities(
        [
            OptimizerButton(c, "optimize_now", "Optimize now", "mdi:magic-staff", c.async_refresh),
            OptimizerButton(c, "apply_proposal", "Apply proposed schedule", "mdi:check-circle", c.async_apply),
            OptimizerButton(c, "revert", "Revert schedule", "mdi:undo", c.scheduler.async_revert),
        ]
    )


class OptimizerButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OptimizerCoordinator,
        key: str,
        name: str,
        icon: str,
        action: Callable[[], Awaitable[None]],
    ) -> None:
        self._action = action
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="HVAC Schedule Optimizer",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        await self._action()
