"""The schedule runner: holds the active schedule and drives the thermostat."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import engine
from .const import DOMAIN, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


class Scheduler:
    """Owns the active schedule and sets the thermostat at each node time.

    It only writes at node transitions (and once when it starts or is enabled),
    so a manual change at the thermostat sticks until the next node.

    Fan: when fan control is configured, a node with fan=True switches the
    thermostat to the circulate mode until the next node. The fan is only
    touched while the schedule uses it (or this scheduler switched it), and it
    goes back to the normal mode on pause and unload.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str, climate_entity: str, default_nodes: list[dict]) -> None:
        self.hass = hass
        self.climate_entity = climate_entity
        self._store: Store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.schedule")
        self._default_nodes = default_nodes
        self.nodes: list[dict] = list(default_nodes)
        self.previous: list[dict] | None = None
        self.source = "baseline"
        self.updated: str | None = None
        self.enabled = False
        self.auto_apply = False
        self.last_set: dict[str, Any] | None = None
        self._unsub: CALLBACK_TYPE | None = None
        self._listeners: list[Callable[[], None]] = []
        # Set by the integration: (normal, circulate) mode names or None, and a humidity guard.
        self.fan_config: Callable[[], tuple[str, str] | None] = lambda: None
        self.fan_blocked: Callable[[], str | None] = lambda: None
        self.fan_forced = False
        self.fan_note: str | None = None

    # ---------------------------------------------------------- persistence
    async def async_load(self) -> None:
        data = await self._store.async_load()
        if not data:
            return
        self.nodes = data.get("nodes") or self.nodes
        self.previous = data.get("previous")
        self.source = data.get("source", self.source)
        self.updated = data.get("updated")
        self.enabled = bool(data.get("enabled", False))
        self.auto_apply = bool(data.get("auto_apply", False))
        self.last_set = data.get("last_set")
        self.fan_forced = bool(data.get("fan_forced", False))

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "nodes": self.nodes,
                "previous": self.previous,
                "source": self.source,
                "updated": self.updated,
                "enabled": self.enabled,
                "auto_apply": self.auto_apply,
                "last_set": self.last_set,
                "fan_forced": self.fan_forced,
            }
        )

    # ---------------------------------------------------------- listeners
    @callback
    def async_add_listener(self, update_callback: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(update_callback)

        @callback
        def remove() -> None:
            self._listeners.remove(update_callback)

        return remove

    @callback
    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    # ---------------------------------------------------------- lifecycle
    async def async_start(self) -> None:
        """Called once HA has started. Catch up to the node that should be active now."""
        if self.enabled:
            await self._async_apply_current()
            self._arm()

    @callback
    def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def async_set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        await self._async_save()
        if enabled:
            await self._async_apply_current()
            self._arm()
        else:
            self.async_stop()
            await self.async_release_fan()
        self._notify()

    async def async_set_auto_apply(self, value: bool) -> None:
        self.auto_apply = value
        await self._async_save()
        self._notify()

    # ---------------------------------------------------------- schedule edits
    async def async_set_nodes(self, nodes: list[dict], source: str) -> None:
        """Replace the active schedule. The old one is kept for one-tap revert."""
        engine.parse_nodes(nodes)  # validates
        clean = [
            {"time": engine.hhmm(engine.to_minutes(n["time"])), "temp": n["temp"], **({"fan": True} if n.get("fan") else {})}
            for n in nodes
        ]
        clean.sort(key=lambda n: engine.to_minutes(n["time"]))
        if clean != self.nodes:
            self.previous = self.nodes
        self.nodes = clean
        self.source = source
        self.updated = dt_util.utcnow().isoformat()
        await self._async_save()
        if self.enabled:
            await self._async_apply_current()
            self._arm()
        self._notify()

    async def async_revert(self) -> None:
        if not self.previous:
            raise HomeAssistantError("There is no previous schedule to go back to.")
        current, self.nodes = self.nodes, self.previous
        self.previous = current
        self.source = "revert"
        self.updated = dt_util.utcnow().isoformat()
        await self._async_save()
        if self.enabled:
            await self._async_apply_current()
            self._arm()
        self._notify()

    # ---------------------------------------------------------- timing
    def current_setpoint(self, now: datetime | None = None) -> float:
        now = dt_util.as_local(now or dt_util.now())
        return engine.setpoint_at(self.nodes, now.hour * 60 + now.minute)

    def next_change(self, now: datetime | None = None) -> tuple[datetime, float]:
        now = dt_util.as_local(now or dt_util.now())
        ahead, temp = engine.next_change(self.nodes, now.hour * 60 + now.minute)
        base = now.replace(second=0, microsecond=0)
        return dt_util.as_local(base + timedelta(minutes=ahead)), temp

    def snapshot(self) -> dict[str, Any]:
        """Plain-data view of the schedule state for the dashboard card."""
        when, temp = self.next_change()
        return {
            "nodes": self.nodes,
            "previous": self.previous,
            "source": self.source,
            "updated": self.updated,
            "enabled": self.enabled,
            "auto_apply": self.auto_apply,
            "setpoint_now": self.current_setpoint(),
            "next_change": when.isoformat(),
            "next_temp": temp,
            "climate_entity": self.climate_entity,
            "fan_now": self.fan_forced,
            "fan_control": self.fan_config() is not None,
            "fan_note": self.fan_note,
        }

    @callback
    def _arm(self) -> None:
        self.async_stop()
        when, _ = self.next_change()
        self._unsub = async_track_point_in_time(self.hass, self._async_fire, when)

    async def _async_fire(self, now: datetime) -> None:
        self._unsub = None
        if self.enabled:
            await self._async_apply_current(now)
            self._arm()
        self._notify()

    # ---------------------------------------------------------- thermostat
    async def _async_apply_current(self, now: datetime | None = None) -> None:
        state = self.hass.states.get(self.climate_entity)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.warning("%s is unavailable, skipping setpoint change", self.climate_entity)
            return
        if state.state != "cool":
            _LOGGER.debug("%s is in mode %s, not cool; leaving it alone", self.climate_entity, state.state)
            return
        await self._async_apply_fan(state, now)
        temp = self.current_setpoint(now)
        lo, hi = state.attributes.get("min_temp"), state.attributes.get("max_temp")
        if lo is not None:
            temp = max(temp, float(lo))
        if hi is not None:
            temp = min(temp, float(hi))
        if state.attributes.get("temperature") is not None and abs(float(state.attributes["temperature"]) - temp) < 1e-6:
            self.last_set = {"temp": temp, "at": dt_util.utcnow().isoformat(), "changed": False}
            return
        try:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": self.climate_entity, "temperature": temp},
                blocking=True,
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Could not set %s to %s: %s", self.climate_entity, temp, err)
            return
        self.last_set = {"temp": temp, "at": dt_util.utcnow().isoformat(), "changed": True}
        await self._async_save()

    async def _async_apply_fan(self, state, now: datetime | None = None) -> None:
        cfg = self.fan_config()
        uses_fan = any(n.get("fan") for n in self.nodes)
        if cfg is None or not (uses_fan or self.fan_forced):
            self.fan_note = None
            return
        normal, circ = cfg
        local = dt_util.as_local(now or dt_util.now())
        want = uses_fan and engine.fan_at(self.nodes, local.hour * 60 + local.minute)
        self.fan_note = None
        if want and (blocked := self.fan_blocked()):
            self.fan_note = blocked
            want = False
        await self._async_set_fan(state, circ if want else normal, want)

    async def _async_set_fan(self, state, mode: str, forced: bool) -> None:
        if state.attributes.get("fan_mode") != mode:
            try:
                await self.hass.services.async_call(
                    "climate", "set_fan_mode", {"entity_id": self.climate_entity, "fan_mode": mode}, blocking=True
                )
            except HomeAssistantError as err:
                _LOGGER.warning("Could not set %s fan to %s: %s", self.climate_entity, mode, err)
                return
        if forced != self.fan_forced:
            self.fan_forced = forced
            await self._async_save()

    async def async_release_fan(self) -> None:
        """Put the fan back to the normal mode if this scheduler switched it to circulate."""
        cfg = self.fan_config()
        state = self.hass.states.get(self.climate_entity)
        if not self.fan_forced or cfg is None or state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return
        await self._async_set_fan(state, cfg[0], False)
