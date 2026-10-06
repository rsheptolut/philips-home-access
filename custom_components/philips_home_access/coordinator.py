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

    def __init__(self, hass: HomeAssistant, client: HomeAccess,
                 state_only: set[str] | None = None) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SLOW_POLL_INTERVAL)
        self.client = client
        # locks the user marked state only (options): no lock entity for them
        self.state_only: set[str] = state_only or set()
        self.locks: dict[str, Lock] = {}          # esn -> latest Lock (metadata)
        self._trackers: dict[str, LockTracker] = {}
        self._realtimes: dict[str, Realtime] = {}   # datacenter code -> listener
        self._ws_tasks: list = []
        self._gateway_kinds_logged: set[tuple[str, str]] = set()

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
            if lock.is_gateway:
                continue  # no bolt, door or battery to track; kept for device info
            tr = self._trackers.get(lock.esn)
            if tr is None:
                self._trackers[lock.esn] = LockTracker(LockState(
                    lock.esn, bolt=lock.open_status, door=lock.door,
                    battery=lock.battery, online=lock.online))
                continue
            changes = tr.apply_poll(lock.open_status, lock.door, lock.battery,
                                    lock.online)
            if any(c.startswith("online=") for c in changes):
                _LOGGER.info("lock %s connectivity -> %s", lock.esn,
                             "online" if lock.online else "OFFLINE")
        self._update_poll_interval()
        _LOGGER.debug("poll: %d lock(s): %s", len(locks),
                      {esn: tr.state.summary() for esn, tr in self._trackers.items()})
        return {esn: tr.state for esn, tr in self._trackers.items()}

    def _update_poll_interval(self) -> bool:
        """Slow-poll only while realtime is genuinely carrying every lock.

        A datacenter with no WS at all is poll-only, and so is one whose socket
        is currently down -- otherwise a dead listener left us on the 15-min
        safety-net poll, which is the slowest path, exactly when we needed the
        fastest one. Likewise while any device is offline: the cloud pushes
        wifiState for the lock itself, but never for an accessory like the
        door sensor, and a push missed while the socket was down is gone --
        only the poll can be counted on to notice the return.
        """
        ws_up = self._ws_covers([l for l in self.locks.values() if not l.is_gateway])
        offline = sorted(esn for esn, tr in self._trackers.items()
                         if not tr.state.online)
        interval = SLOW_POLL_INTERVAL if ws_up and not offline else FAST_POLL_INTERVAL
        if interval != self.update_interval:
            _LOGGER.info("poll interval -> %s (realtime %s%s)", interval,
                         "up" if ws_up else "down",
                         f", offline: {', '.join(offline)}" if offline else "")
            self.update_interval = interval
            return True
        return False

    # -- device roles -------------------------------------------------------
    def lock_esns(self) -> list[str]:
        """The esns that are actually locks -- accessories filtered out.

        device/list returns paired accessories (the door sensor) alongside the
        lock, so platforms that only make sense for a lock use this instead of
        iterating every esn in `data`. (Gateways never reach `data` at all.)
        """
        return [esn for esn in self.data
                if not (esn in self.locks and self.locks[esn].is_accessory)]

    # -- commands -----------------------------------------------------------
    @callback
    def set_pending(self, esn: str, pending: str | None) -> None:
        """Show a command as in flight ("locking"/"unlocking") or clear it.

        Notifies entities without async_set_updated_data, which would also
        push the next scheduled poll back.
        """
        tr = self._trackers.get(esn)
        if tr is not None and tr.set_pending(pending):
            self.async_update_listeners()

    @callback
    def expire_pending(self, esn: str) -> None:
        """Give up on a command nothing has confirmed (see PENDING_TIMEOUT)."""
        tr = self._trackers.get(esn)
        if tr is not None and tr.expire_pending():
            self.async_update_listeners()

    def ws_covers_lock(self, esn: str) -> bool:
        """True when a live WebSocket will confirm this lock's commands."""
        lock = self.locks.get(esn)
        return lock is not None and self._ws_covers([lock])

    # -- realtime -----------------------------------------------------------
    def _ws_covers(self, locks: list[Lock]) -> bool:
        """True when a live WebSocket is carrying events for every lock.

        A lock behind a gateway never counts: whether its events arrive, and
        under whose esn, is unverified -- so it gets the fast poll.
        """
        for lock in locks:
            if self.client.gateway_of(lock) is not None:
                return False
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
            dev = self.locks.get(ev.lock_id)
            if dev is not None and dev.is_gateway:
                # Unverified territory: do a gateway lock's events arrive under
                # the gateway's esn? Surface each kind once so a user's log
                # answers it.
                key = (ev.lock_id, ev.kind)
                if key not in self._gateway_kinds_logged:
                    self._gateway_kinds_logged.add(key)
                    _LOGGER.info("ws event %s from gateway %s (not applied; please "
                                 "report on GitHub): %s", ev.kind, ev.lock_id,
                                 str(ev.raw)[:400])
                return
            _LOGGER.debug("ws event for unknown lock %s (ignored)", ev.lock_id)
            return
        res = tr.apply(ev)
        _LOGGER.debug("ws event %-7s state=%-8s msgId=%s ts=%s -> "
                      "stale=%s dup=%s changes=%s | %s",
                      ev.kind, ev.state, ev.msg_id, ev.timestamp,
                      res.stale, res.duplicate, res.changes, tr.state.summary())
        # Push to entities only when something actually changed (changes also
        # carries pending transitions, so setLock still surfaces locking/...).
        if not res.changes:
            return
        if self._update_poll_interval():
            # a pushed connectivity change moved the poll rate:
            # async_set_updated_data reschedules the poll on the new interval
            self.async_set_updated_data(
                {esn: t.state for esn, t in self._trackers.items()})
        else:
            # `data` holds these same LockState objects, so entities only need
            # telling. async_set_updated_data would also restart the poll
            # timer -- on a busy day pushing the safety-net poll, the one thing
            # that corrects a wrong or missed event, back indefinitely.
            self.async_update_listeners()

    async def async_stop_realtime(self) -> None:
        for task in self._ws_tasks:
            task.cancel()
        self._ws_tasks.clear()
        self._realtimes.clear()
