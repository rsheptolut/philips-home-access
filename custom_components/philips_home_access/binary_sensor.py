"""Binary sensor platform: door contacts and read-only lock state."""
from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
    # The door contact is reported on the lock, not on the sensor accessory's
    # own record -- one door entity per lock, none for the accessory.
    entities: list[BinarySensorEntity] = [
        PhilipsDoorSensor(coordinator, esn) for esn in coordinator.lock_esns()
    ]
    # LockEntity always exposes lock/unlock services. A LOCK binary sensor is
    # the truthful read-only representation when the device record reports
    # isRemoteUnlock=0 (on = unlocked, off = locked in Home Assistant).
    entities.extend(
        PhilipsLockStateSensor(coordinator, esn)
        for esn in coordinator.read_only_lock_esns()
    )
    async_add_entities(entities)


class PhilipsDoorSensor(PhilipsLockEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.DOOR
    _attr_name = "Door"

    def __init__(self, coordinator: PhilipsCoordinator, esn: str) -> None:
        super().__init__(coordinator, esn)
        self._attr_unique_id = f"{esn}_door"

    @property
    def is_on(self) -> bool | None:
        st = self._lock_state
        return st.door == "open" if st and st.door else None


class PhilipsLockStateSensor(PhilipsLockEntity, BinarySensorEntity):
    """Read-only bolt state for devices that cannot be remotely controlled."""

    _attr_device_class = BinarySensorDeviceClass.LOCK
    _attr_name = "Lock"

    def __init__(self, coordinator: PhilipsCoordinator, esn: str) -> None:
        super().__init__(coordinator, esn)
        self._attr_unique_id = f"{esn}_lock_state"

    @property
    def is_on(self) -> bool | None:
        st = self._lock_state
        return st.bolt == "unlocked" if st and st.bolt else None
