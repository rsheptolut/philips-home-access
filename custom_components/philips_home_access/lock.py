"""Lock platform: open/close the deadbolt."""
from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import PhilipsCoordinator
from .entity import PhilipsLockEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PhilipsCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(PhilipsLock(coordinator, esn) for esn in coordinator.data)


class PhilipsLock(PhilipsLockEntity, LockEntity):
    _attr_name = None  # the lock is the device's primary entity

    def __init__(self, coordinator: PhilipsCoordinator, esn: str) -> None:
        super().__init__(coordinator, esn)
        self._attr_unique_id = f"{esn}_lock"

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
        await self.coordinator.client.async_lock(self._esn)

    async def async_unlock(self, **kwargs: Any) -> None:
        await self.coordinator.client.async_unlock(self._esn)
