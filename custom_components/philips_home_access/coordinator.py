"""Coordinator: safety-net poll + realtime WebSocket, feeding a per-lock tracker."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, UPDATE_INTERVAL
from .homeaccess import (
    AuthError,
    Datacenter,
    HomeAccess,
    HomeAccessConnectionError,
    Lock,
    LockEvent,
    LockState,
    LockTracker,
)

_LOGGER = logging.getLogger(__name__)


class PhilipsCoordinator(DataUpdateCoordinator[dict[str, LockState]]):
    """Holds the current state of every lock on the account."""

    def __init__(self, hass: HomeAssistant, client: HomeAccess) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.client = client
        self.locks: dict[str, Lock] = {}          # esn -> latest Lock (metadata)
        self._trackers: dict[str, LockTracker] = {}
        self._ws_tasks: list = []

    # -- safety-net poll ----------------------------------------------------
    async def _async_update_data(self) -> dict[str, LockState]:
        try:
            locks = await self.client.async_discover()
        except AuthError as e:
            raise ConfigEntryAuthFailed(str(e)) from e
        except HomeAccessConnectionError as e:
            raise UpdateFailed(str(e)) from e
        for lock in locks:
            self.locks[lock.esn] = lock
            tr = self._trackers.get(lock.esn)
            if tr is None:
                self._trackers[lock.esn] = LockTracker(LockState(
                    lock.esn, bolt=lock.open_status, door=lock.door,
                    battery=lock.battery))
            else:
                # A poll is authoritative for current bolt/battery; keep door if
                # the poll can't determine it (door is event-driven).
                if lock.open_status:
                    tr.state.bolt = lock.open_status
                if lock.door:
                    tr.state.door = lock.door
                if lock.battery is not None:
                    tr.state.battery = lock.battery
        _LOGGER.debug("poll: %d lock(s): %s", len(locks),
                      {esn: tr.state.summary() for esn, tr in self._trackers.items()})
        return {esn: tr.state for esn, tr in self._trackers.items()}

    # -- realtime -----------------------------------------------------------
    async def async_start_realtime(self) -> None:
        """One WebSocket listener per WebSocket-capable datacenter."""
        locks = await self.client.async_locks()
        codes = sorted({l.datacenter_code for l in locks
                        if Datacenter.by_code(l.datacenter_code).ws_addr})
        for code in codes:
            rt = self.client.realtime(code)
            self._ws_tasks.append(self.hass.async_create_background_task(
                rt.listen(on_event=self._on_event), name=f"{DOMAIN}_ws_{code}"))

    @callback
    def _on_event(self, ev: LockEvent) -> None:
        tr = self._trackers.get(ev.lock_id)
        if tr is None:
            _LOGGER.debug("ws event for unknown lock %s (ignored)", ev.lock_id)
            return
        res = tr.apply(ev)
        _LOGGER.debug("ws event %-7s state=%-8s msgId=%s ts=%s -> "
                      "stale=%s dup=%s changes=%s | %s",
                      ev.kind, ev.state, ev.msg_id, ev.timestamp,
                      res.stale, res.duplicate, res.changes, tr.state.summary())
        # Push to entities only when something actually changed (changes also
        # carries pending transitions, so setLock still surfaces locking/...).
        if res.changes:
            self.async_set_updated_data(
                {esn: t.state for esn, t in self._trackers.items()})

    async def async_stop_realtime(self) -> None:
        for task in self._ws_tasks:
            task.cancel()
        self._ws_tasks.clear()
