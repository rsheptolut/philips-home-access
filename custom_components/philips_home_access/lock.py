"""Lock platform: open/close the deadbolt."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, FOLLOW_UP_REFRESHES
from .coordinator import PhilipsCoordinator
from .entity import PhilipsLockEntity
from .homeaccess import CommandError, HomeAccessError
from .homeaccess.tracker import PENDING_TIMEOUT

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PhilipsCoordinator = hass.data[DOMAIN][entry.entry_id]
    # Accessories (the door sensor) have no bolt -- no lock entity for them;
    # nor do locks marked state only (they get a read-only lock sensor).
    async_add_entities(PhilipsLock(coordinator, esn)
                       for esn in coordinator.lock_esns()
                       if esn not in coordinator.state_only)


class PhilipsLock(PhilipsLockEntity, LockEntity):
    _attr_name = None  # the lock is the device's primary entity

    def __init__(self, coordinator: PhilipsCoordinator, esn: str) -> None:
        super().__init__(coordinator, esn)
        self._attr_unique_id = f"{esn}_lock"
        self._timers: set[Callable[[], None]] = set()

    @property
    def is_locked(self) -> bool | None:
        st = self._lock_state
        return st.bolt == "locked" if st and st.bolt else None

    @property
    def is_locking(self) -> bool:
        st = self._lock_state
        return bool(st and st.pending == "locking")

    @property
    def is_unlocking(self) -> bool:
        st = self._lock_state
        return bool(st and st.pending == "unlocking")

    async def async_lock(self, **kwargs: Any) -> None:
        await self._command("lock", "locking", self.coordinator.client.async_lock)

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._command("unlock", "unlocking", self.coordinator.client.async_unlock)

    async def _command(self, verb: str, pending: str, send) -> None:
        coordinator = self.coordinator
        # Show "locking..." straight away. Over a WebSocket the lock's own
        # events confirm the move within seconds; without one (no realtime in
        # this datacenter, socket down, gateway lock) only a poll can, so ask
        # for a couple soon after rather than waiting out the 60 s interval.
        coordinator.set_pending(self._esn, pending)
        try:
            await send(self._esn)
        except CommandError as e:
            coordinator.set_pending(self._esn, None)
            # The cloud answered and said no. Some locks can't be operated
            # remotely at all (the Philips app offers no buttons for them);
            # point at the way out rather than at host names. The details
            # still go to the log.
            _LOGGER.warning("%s refused for %s: %s", verb, self._esn, e)
            raise HomeAssistantError(
                f"Philips didn't accept a remote {verb} for {self._name()}. "
                f"If the Philips app can't {verb} this lock either, mark it "
                f"state only: Settings > Devices & services > Philips Home "
                f"Access > Configure.") from e
        except HomeAccessError as e:
            coordinator.set_pending(self._esn, None)
            # HomeAssistantError shows the reason to the user; anything else
            # would surface as an "Unexpected exception" traceback.
            raise HomeAssistantError(f"Could not {verb} {self._name()}: {e}") from e
        if not coordinator.ws_covers_lock(self._esn):
            for delay in FOLLOW_UP_REFRESHES:
                self._later(delay, lambda: self.hass.async_create_task(
                    coordinator.async_request_refresh()))
        self._later(PENDING_TIMEOUT + 1,
                    lambda: coordinator.expire_pending(self._esn))

    def _name(self) -> str:
        lock = self.coordinator.locks.get(self._esn)
        return lock.nickname if lock and lock.nickname else self._esn

    def _later(self, delay: float, action: Callable[[], Any]) -> None:
        """Run `action` on the event loop after `delay` s, unless removed first."""
        @callback
        def _fire(_now: datetime) -> None:
            self._timers.discard(cancel)
            action()

        cancel = async_call_later(self.hass, delay, _fire)
        self._timers.add(cancel)

    async def async_will_remove_from_hass(self) -> None:
        for cancel in self._timers:
            cancel()
        self._timers.clear()
        await super().async_will_remove_from_hass()
