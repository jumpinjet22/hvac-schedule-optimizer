"""The schedule runner: holds the active schedule and drives the thermostat."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Context, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_point_in_time, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import engine
from .const import DOMAIN, STORAGE_VERSION, run_mode, setpoints_of

MODE_HOLD_S = 3 * 3600  # auto switching keeps a mode at least this long
MANUAL_MODE_PAUSE_S = 12 * 3600  # and stays away for this long after someone picks a mode by hand

_LOGGER = logging.getLogger(__name__)


class Scheduler:
    """Owns the active schedule and sets the thermostat at each node time.

    It only writes at node transitions (and once when it starts or is enabled),
    so a manual change at the thermostat sticks until the next node.

    Fan: when fan control is configured, a node with fan=True switches the
    thermostat to the circulate mode until the next node. The fan is only
    touched while the schedule uses it (or this scheduler switched it), and it
    goes back to the normal mode on pause and unload.

    Manual changes: every write here carries a context of its own, so a setpoint
    change with any other context (the app, the wall, the vendor's app) is reported
    to on_manual_change for comfort learning.

    Modes: in cool it writes the cool setpoint, in heat the heat setpoint (when the
    schedule has heat values), in heat_cool both, never closer than deadband(). Other
    modes (off, dry, fan only) are left alone. With auto switching opted in, the
    coordinator asks for heat or cool through async_auto_switch.
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
        self.on_manual_change: Callable[..., Awaitable[None]] | None = None
        self.deadband: Callable[[], float] = lambda: 0.0
        self._own_ctx: deque[str] = deque(maxlen=20)
        self.mode_switched_at: float | None = None
        self.manual_mode_at: float | None = None
        self.mode_note: str | None = None

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
    def async_listen(self) -> CALLBACK_TYPE:
        """Watch the thermostat for setpoint changes this scheduler did not make."""
        return async_track_state_change_event(self.hass, [self.climate_entity], self._async_state_changed)

    def _context(self) -> Context:
        ctx = Context()
        self._own_ctx.append(ctx.id)
        return ctx

    def _ours(self, ctx) -> bool:
        return ctx.id in self._own_ctx or bool(ctx.parent_id and ctx.parent_id in self._own_ctx)

    def _echo(self, key: str, value: float) -> bool:
        """A cloud thermostat reporting our own recent write back with a fresh context."""
        if not self.last_set or self.last_set.get(key) is None or abs(float(self.last_set[key]) - value) > 1e-6:
            return False
        set_at = dt_util.parse_datetime(self.last_set["at"])
        return bool(set_at and (dt_util.utcnow() - set_at).total_seconds() < 120)

    async def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        if old is None or new is None:
            return
        if old.state != new.state:
            if not self._ours(new.context) and new.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN) and old.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                self.manual_mode_at = dt_util.utcnow().timestamp()  # someone picked a mode: auto switching backs off
            return
        if run_mode(new) is None or not self.enabled or self.on_manual_change is None or self._ours(new.context):
            return
        source = "ui" if new.context.user_id else "device"
        for (before, after), side, key in zip(zip(setpoints_of(old), setpoints_of(new)), ("cool", "heat"), ("temp", "heat")):
            if before is None or after is None or abs(before - after) < 1e-6 or self._echo(key, after):
                continue
            await self.on_manual_change(before, after, source, side)

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
            {
                "time": engine.hhmm(engine.to_minutes(n["time"])),
                "temp": n["temp"],
                **({"heat": n["heat"]} if n.get("heat") is not None else {}),
                **({"fan": True} if n.get("fan") else {}),
            }
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

    def current_heat(self, now: datetime | None = None) -> float | None:
        """Heat setpoint the schedule asks for now, or None when the schedule has no heat values."""
        plan = engine.heat_plan(self.nodes)
        if not plan:
            return None
        now = dt_util.as_local(now or dt_util.now())
        return engine._step_at(plan, now.hour * 60 + now.minute)

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
            "heat_now": self.current_heat(),
            "mode_note": self.mode_note,
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
        mode = run_mode(state)
        if mode is None:
            _LOGGER.debug("%s is in mode %s; leaving it alone", self.climate_entity, state.state)
            return
        await self._async_apply_fan(state, now)
        lo, hi = state.attributes.get("min_temp"), state.attributes.get("max_temp")

        def clamp(t: float) -> float:
            if lo is not None:
                t = max(t, float(lo))
            if hi is not None:
                t = min(t, float(hi))
            return t

        cool_now, heat_now = setpoints_of(state)
        cool = clamp(self.current_setpoint(now))
        heat = self.current_heat(now)
        heat = clamp(heat) if heat is not None else None
        if mode == "cool":
            data, want = {"temperature": cool}, (cool, None)
        elif mode == "heat":
            if heat is None:
                return  # the schedule has no heat values: the thermostat keeps its own
            data, want = {"temperature": heat}, (None, heat)
        else:
            low = heat if heat is not None else heat_now
            if low is None:
                return
            cool = max(cool, low + self.deadband())  # thermostats refuse a pair closer than their gap
            data, want = {"target_temp_high": cool, "target_temp_low": low}, (cool, low)
        record = {"temp": want[0], "heat": want[1], "at": dt_util.utcnow().isoformat()}
        same = all(
            w is None or (c is not None and abs(c - w) < 1e-6) for w, c in zip(want, (cool_now, heat_now))
        )
        if same:
            self.last_set = {**record, "changed": False}
            return
        try:
            await self.hass.services.async_call(
                "climate",
                "set_temperature",
                {"entity_id": self.climate_entity, **data},
                blocking=True,
                context=self._context(),
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Could not set %s to %s: %s", self.climate_entity, data, err)
            return
        self.last_set = {**record, "changed": True}
        await self._async_save()

    async def async_auto_switch(self, want: str | None, reason: str = "") -> None:
        """Switch the thermostat to heat or cool when the plan needs it, with safeguards.

        Holds a mode at least MODE_HOLD_S, backs off for MANUAL_MODE_PAUSE_S after someone
        picks a mode by hand, and never touches a thermostat that is off.
        """
        state = self.hass.states.get(self.climate_entity)
        if not self.enabled or want not in ("heat", "cool") or state is None or state.state not in ("heat", "cool"):
            return
        if state.state == want:
            return
        now = dt_util.utcnow().timestamp()
        if self.manual_mode_at and now - self.manual_mode_at < MANUAL_MODE_PAUSE_S:
            self.mode_note = "Mode was changed by hand, so switching is paused for 12 hours"
            return
        if self.mode_switched_at and now - self.mode_switched_at < MODE_HOLD_S:
            return
        try:
            await self.hass.services.async_call(
                "climate", "set_hvac_mode", {"entity_id": self.climate_entity, "hvac_mode": want},
                blocking=True, context=self._context(),
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Could not switch %s to %s: %s", self.climate_entity, want, err)
            return
        self.mode_switched_at = now
        self.mode_note = f"Switched to {want}{': ' + reason if reason else ''}"
        await self._async_apply_current()
        self._notify()

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
                    "climate",
                    "set_fan_mode",
                    {"entity_id": self.climate_entity, "fan_mode": mode},
                    blocking=True,
                    context=self._context(),
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
