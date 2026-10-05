"""Shared base entity (device grouping + availability)."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PhilipsCoordinator
from .homeaccess import Lock, LockState


def device_info_for(esn: str, lock: Lock | None) -> DeviceInfo:
    """The HA device for one esn (lock, accessory or gateway).

    Parent links (accessory -> lock, lock -> gateway) are not set here:
    DeviceInfo's via_device is deprecated, so __init__ links devices by id.
    """
    raw = lock.raw if lock else {}
    return DeviceInfo(
        identifiers={(DOMAIN, esn)},
        name=lock.nickname if lock and lock.nickname else esn,
        manufacturer="Philips",
        model=raw.get("productModel"),
        sw_version=(raw.get("lockSoftwareVersion") or raw.get("gatewayVersion")
                    or raw.get("wifiVersion")),
        serial_number=esn,
    )


class PhilipsLockEntity(CoordinatorEntity[PhilipsCoordinator]):
    """Base for all entities of one lock (device = the lock, keyed by esn)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PhilipsCoordinator, esn: str) -> None:
        super().__init__(coordinator)
        self._esn = esn

    @property
    def _lock_state(self) -> LockState | None:
        return self.coordinator.data.get(self._esn)

    @property
    def available(self) -> bool:
        st = self._lock_state
        return super().available and st is not None and st.online

    @property
    def device_info(self) -> DeviceInfo:
        return device_info_for(self._esn, self.coordinator.locks.get(self._esn))
