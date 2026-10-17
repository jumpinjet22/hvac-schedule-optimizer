"""Sensors: status, proposed and active schedule, estimated change, model match."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OptimizerCoordinator


def _nodes_text(nodes: list[dict] | None) -> str | None:
    if not nodes:
        return None
    return ", ".join(f"{n['time']} {n['temp']}°" for n in nodes)


@dataclass(frozen=True, kw_only=True)
class OptimizerSensorDescription(SensorEntityDescription):
    value_fn: Callable[[OptimizerCoordinator], Any]
    attrs_fn: Callable[[OptimizerCoordinator], dict[str, Any]] = lambda c: {}


def _data(c: OptimizerCoordinator) -> dict[str, Any]:
    return c.data or {}


DESCRIPTIONS: tuple[OptimizerSensorDescription, ...] = (
    OptimizerSensorDescription(
        key="status",
        name="Status",
        icon="mdi:thermostat-auto",
        value_fn=lambda c: _data(c).get("status", "waiting"),
        attrs_fn=lambda c: {
            "message": _data(c).get("message"),
            "generated_at": _data(c).get("generated_at"),
            "candidates": _data(c).get("candidates"),
            "comfortable_candidates": _data(c).get("comfortable"),
            "model": _data(c).get("model"),
            "validation": _data(c).get("validation"),
            "rooms": _data(c).get("rooms"),
            "outdoor_sensors": _data(c).get("outdoor_sensors"),
            "start_temp": _data(c).get("start_temp"),
            "horizon_hours": _data(c).get("horizon_hours"),
        },
    ),
    OptimizerSensorDescription(
        key="proposed_schedule",
        name="Proposed schedule",
        icon="mdi:calendar-clock",
        value_fn=lambda c: _nodes_text((_data(c).get("best") or {}).get("nodes")) or "none",
        attrs_fn=lambda c: {
            "nodes": (_data(c).get("best") or {}).get("nodes"),
            "ac_hours": (_data(c).get("best") or {}).get("ac_hours"),
            "temp_min": (_data(c).get("best") or {}).get("temp_min"),
            "temp_max": (_data(c).get("best") or {}).get("temp_max"),
            "closest_if_none_comfortable": (_data(c).get("closest") or {}).get("nodes"),
        },
    ),
    OptimizerSensorDescription(
        key="active_schedule",
        name="Active schedule",
        icon="mdi:calendar-check",
        value_fn=lambda c: _nodes_text(c.scheduler.nodes),
        attrs_fn=lambda c: {
            "nodes": c.scheduler.nodes,
            "source": c.scheduler.source,
            "updated": c.scheduler.updated,
            "previous": c.scheduler.previous,
            "running": c.scheduler.enabled,
            "last_set": c.scheduler.last_set,
            "current_setpoint": c.scheduler.current_setpoint(),
            "next_change": c.scheduler.next_change()[0].isoformat(),
            "next_setpoint": c.scheduler.next_change()[1],
        },
    ),
    OptimizerSensorDescription(
        key="estimated_change",
        name="Estimated energy change",
        icon="mdi:leaf",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda c: _data(c).get("energy_change_pct"),
        attrs_fn=lambda c: {
            "baseline_ac_hours": (_data(c).get("baseline") or {}).get("ac_hours"),
            "proposed_ac_hours": (_data(c).get("best") or {}).get("ac_hours"),
            "note": "Relative compressor energy estimate for the next 24h, not kWh.",
        },
    ),
    OptimizerSensorDescription(
        key="model_match",
        name="Model match",
        icon="mdi:chart-line",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda c: (_data(c).get("validation") or {}).get("ratio"),
        attrs_fn=lambda c: {
            "predicted_ac_hours_per_day": (_data(c).get("validation") or {}).get("predicted_hours"),
            "actual_ac_hours_per_day": (_data(c).get("validation") or {}).get("actual_hours"),
            "days_checked": (_data(c).get("validation") or {}).get("days"),
            "note": "Predicted over actual AC runtime for recent days. Near 1.0 means the house model is trustworthy.",
        },
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: OptimizerCoordinator = entry.runtime_data.coordinator
    async_add_entities(OptimizerSensor(coordinator, d) for d in DESCRIPTIONS)


class OptimizerSensor(CoordinatorEntity[OptimizerCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: OptimizerSensorDescription

    def __init__(self, coordinator: OptimizerCoordinator, description: OptimizerSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="HVAC Schedule Optimizer",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Active schedule and last-set info change without a coordinator refresh.
        self.async_on_remove(self.coordinator.scheduler.async_add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.entity_description.attrs_fn(self.coordinator)
