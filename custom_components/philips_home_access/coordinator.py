"""Coordinator: safety-net poll + realtime WebSocket, feeding a per-lock tracker."""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, FAST_POLL_INTERVAL, SLOW_POLL_INTERVAL
from .homeaccess import (
    AuthError,
    Datacenter,
    HomeAccess,
    HomeAccessConnectionError,
    Lock,
    LockEvent,
    LockState,
    LockTracker,
    Realtime,
)

_LOGGER = logging.getLogger(__name__)


class PhilipsCoordinator(DataUpdateCoordinator[dict[str, LockState]]):
    """Holds the current state of every lock on the account."""

    def __init__(self, hass: HomeAssistant, client: HomeAccess) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SLOW_POLL_INTERVAL)
        self.client = client
        self.locks: dict[str, Lock] = {}          # esn -> latest Lock (metadata)
        self._trackers: dict[str, LockTracker] = {}
        self._realtimes: dict[str, Realtime] = {}   # datacenter code -> listener
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
                    battery=lock.battery, online=lock.online))
            else:
                # A poll is authoritative for current bolt/battery; keep door if
                # the poll can't determine it (door is event-driven).
                if lock.open_status:
                    tr.state.bolt = lock.open_status
                if lock.door:
                    tr.state.door = lock.door
                if lock.battery is not None:
                    tr.state.battery = lock.battery
                # online is always a definite bool (unlike bolt/door/battery,
                # never "undetermined"), and it's the cloud's own freshest
                # word on whether it can currently reach the device at all --
                # trust it outright, every poll.
                if lock.online != tr.state.online:
                    _LOGGER.info("lock %s connectivity -> %s", lock.esn,
                                 "online" if lock.online else "OFFLINE")
                    tr.state.online = lock.online
        # Slow-poll only while realtime is genuinely carrying every lock. A
        # datacenter with no WS at all is poll-only, and so is one whose socket
        # is currently down -- otherwise a dead listener left us on the 15-min
        # safety-net poll, which is the slowest path, exactly when we needed
        # the fastest one.
        interval = SLOW_POLL_INTERVAL if self._ws_covers(locks) else FAST_POLL_INTERVAL
        if interval != self.update_interval:
            _LOGGER.info("poll interval -> %s (realtime %s)", interval,
                         "up" if interval == SLOW_POLL_INTERVAL else "down")
            self.update_interval = interval
        _LOGGER.debug("poll: %d lock(s): %s", len(locks),
                      {esn: tr.state.summary() for esn, tr in self._trackers.items()})
        return {esn: tr.state for esn, tr in self._trackers.items()}

    # -- device roles -------------------------------------------------------
    def lock_esns(self) -> list[str]:
        """The esns that are actually locks -- accessories filtered out.

        device/list returns paired accessories (the door sensor) alongside the
        lock, so platforms that only make sense for a lock use this instead of
        iterating every esn in `data`.
        """
        return [esn for esn in self.data
                if not (esn in self.locks and self.locks[esn].is_accessory)]

    def controllable_lock_esns(self) -> list[str]:
        """Locks for which the cloud advertises remote control support."""
        return [esn for esn in self.lock_esns()
                if esn not in self.locks
                or self.locks[esn].remote_control_supported]

    def read_only_lock_esns(self) -> list[str]:
        """Locks whose cloud record explicitly disables remote control."""
        return [esn for esn in self.lock_esns()
                if esn in self.locks
                and not self.locks[esn].remote_control_supported]

    # -- realtime -----------------------------------------------------------
    def _ws_covers(self, locks: list[Lock]) -> bool:
        """True when a live WebSocket is carrying events for every lock."""
        for lock in locks:
            dc = Datacenter.by_code(lock.datacenter_code)
            rt = self._realtimes.get(dc.code)
            if not dc.ws_addr or rt is None or not rt.connected:
                return False
        return True

    async def async_start_realtime(self) -> None:
        """One WebSocket listener per WebSocket-capable datacenter."""
        locks = await self.client.async_locks()
        codes = sorted({l.datacenter_code for l in locks
                        if Datacenter.by_code(l.datacenter_code).ws_addr})
        for code in codes:
            rt = self.client.realtime(code)
            self._realtimes[code] = rt
            self._ws_tasks.append(self.hass.async_create_background_task(
                rt.listen(on_event=self._on_event, on_connect=self._on_ws_connect,
                          on_disconnect=self._on_ws_disconnect),
                name=f"{DOMAIN}_ws_{code}"))

    async def _on_ws_connect(self) -> None:
        """Resync on every (re)connect so a drop's missed events are caught."""
        await self.async_request_refresh()

    async def _on_ws_disconnect(self) -> None:
        """Refresh now so the interval drops to the fast poll while we're blind.

        The poll is a weak substitute for realtime: it can refresh the bolt from
        device/list, but only on its own interval, and live door open/close
        arrives *only* as eventType-4 records over the WS (device/list's
        magneticStatus is best-effort -- see research/FINDINGS.md). So while the
        socket is down the door stops moving and the bolt goes stale; poll as
        hard as we can until it is back.
        """
        await self.async_request_refresh()

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
        self._realtimes.clear()
