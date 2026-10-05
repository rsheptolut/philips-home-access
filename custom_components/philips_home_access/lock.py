"""Lock platform: open/close the deadbolt."""
from __future__ import annotations

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
from .homeaccess import HomeAccessError
from .homeaccess.tracker import PENDING_TIMEOUT


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PhilipsCoordinator = hass.data[DOMAIN][entry.entry_id]
    # Accessories (the door sensor) have no bolt -- no lock entity for them.
    async_add_entities(PhilipsLock(coordinator, esn)
                       for esn in coordinator.lock_esns())


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
        except HomeAccessError as e:
            coordinator.set_pending(self._esn, None)
            # HomeAssistantError shows the reason to the user; anything else
            # would surface as an "Unexpected exception" traceback.
            raise HomeAssistantError(f"Could not {verb} {self._esn}: {e}") from e
        if not coordinator.ws_covers_lock(self._esn):
            for delay in FOLLOW_UP_REFRESHES:
                self._later(delay, lambda: self.hass.async_create_task(
                    coordinator.async_request_refresh()))
        self._later(PENDING_TIMEOUT + 1,
                    lambda: coordinator.expire_pending(self._esn))

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
